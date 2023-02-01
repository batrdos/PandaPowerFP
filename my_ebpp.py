import json
import os
import sys

import pandapower as pp
import pandas as pd
from flask import Flask, request
from dotenv import load_dotenv
from pandapower import LoadflowNotConverged

import pandapower as pp
import pandapower.estimation as est
from crypt import methods

import utils
from errors import ConvError, InvalidError, JsonError, PPError

from flask import Flask, request, render_template, jsonify



app = Flask(__name__)

# ERROR HANDLING

@app.errorhandler(InvalidError)
def invalid_error(error):
    """Replies with an invalid error message"""
    return json.dumps(error.to_dict())

@app.errorhandler(JsonError)
def json_error(error):
    """Replies with an json error message"""
    return json.dumps(error.to_dict())

@app.errorhandler(PPError)
def pp_error(error):
    """Replies with a pandapower error message"""
    return json.dumps(error.to_dict())

@app.errorhandler(ConvError)
def conv_error(error):
    """Reples with a pandapower convergence error"""
    return json.dumps(error.to_dict())

# API ENTRY POINTS

@app.route("/")
def index():
    return "Welcome to Electric Blocks Panda Powers."

@app.route("/api", methods=["GET", "POST"])
def api():
    try:
        json.loads(request.data)
    except:
        raise JsonError("Could not parse json from request data")

    status = utils.get_or_error("status", request.json)
    if status == "SIM_REQUEST":
        return sim_request(request.json)
    elif status == "KEEP_ALIVE":
        return keep_alive()
    elif status == "POWERFLOW":
        return powerflow(request.json)
    elif status == "ESTIMATION":
        return estimation(request.json)  
    else: 
        raise InvalidError(f"Status \"{status}\" is not a valid status code.")


# RESPONSES

def keep_alive():
    message = {}
    message["status"] = "KEEP_ALIVE"
    message["response"] = "Keep alive request acknowledged"
    return json.dumps(message)

def sim_request(data):
    is_three_phase = utils.get_or_error("3phase", data)
    elements_dict = utils.get_or_error("elements", data)
    buses = {} # Used for matching bus UUIDs to index

    def process_potential_bus(key, value):
        """ Inner method for processing a positional argument that could be a bus
            This function checks if the value is in the bus keys. This should never cause issues so long as UUID's aren't used for
            any other purpose except for bus identification and as long as there are no UUID collisions. Both of those cases seem
            exceptionally unlikely, so this should work fine.
        """
        if value in buses.keys():
            return buses[value]
        else:
            return value

    bus_list = [(uuid, element) for uuid, element in elements_dict.items() if utils.get_or_error("etype", element) == "bus"]
    element_list = [(uuid, element) for uuid, element in elements_dict.items() if utils.get_or_error("etype", element) != "bus" and utils.get_or_error("etype", element) != "switch"]
    switch_list = [(uuid, element) for uuid, element in elements_dict.items() if utils.get_or_error("etype", element) == "switch"]

    net = pp.create_empty_network()

    for uuid, bus in bus_list:
        element_type = "bus"
        req_props = utils.required_props[element_type]
        positional_args = [ value for key, value in bus.items() if key in req_props ]
        optional_args = { key: value for key, value in bus.items() if (not key in req_props) and (not key == "etype")}
        index = pp.create_bus(net, *positional_args, **optional_args, name=uuid)
        buses[uuid] = index
    
    for uuid, element in element_list:
        element_type = utils.get_or_error("etype", element)
        req_props = utils.required_props[element_type]
        positional_args = [process_potential_bus(key, value) for key, value in element.items() if key in req_props]
        optional_args = { key: value for key, value in element.items() if (not key in req_props) and (not key == "etype")}
        
        if element_type == "load":
            pp.create_load(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "gen":
            pp.create_gen(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "ext_grid":
            pp.create_ext_grid(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "line":
            pp.create_line(net, *positional_args, *optional_args, name=uuid)
        elif element_type == "trafo":
            pp.create_transformer_from_parameters(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "storage":
            pp.create_storage(net, *positional_args, **optional_args, name=uuid)
        else:
            raise InvalidError(f"Element type {element_type} is invalid or not implemented!")

    for uuid, switch in switch_list:
        element_type = "switch"
        req_props = utils.required_props[element_type]
        positional_args = [process_potential_bus(key, value) for key, value in element.items() if key in req_props]
        optional_args = { key: value for key, value in element.items() if (not key in req_props) and (not key == "etype")}
        et = positional_args[2]
        if et == "b":
            pass # This is handled by process_potential_buses
        if et == "l":
            positional_args[1] = pp.get_element_index(net, "line", positional_args[1])
        elif et == "t":
            positional_args[1] = pp.get_element_index(net, "trafo", positional_args[1])
        elif et == "t3":
            positional_args[1] = pp.get_element_index(net, "trafo3w", positional_args[1])
        else:
            raise InvalidError(f"Invalid element type {et}. Must be b,l,t, or t3.")
        pp.create_switch(net, *positional_args, **optional_args, name=uuid)
            
    try:
        if is_three_phase:
            pp.runpp_3ph(net)
        else:
            pp.runpp(net)
    except LoadflowNotConverged:
        report = pp.diagnostic(net, report_style="compact", warnings_only=True)
        raise ConvError("Load flow did not converge.")
    except (KeyError, ValueError) as e:
        raise PPError(str(e))
    except Exception as e:
        raise PPError("Unknown exception has occured: " + str(e))

    message = {}
    message["status"] = "SIM_RESULT"
    results = {}

    for uuid,element in elements_dict.items():
        element_type = elements_dict[uuid]["etype"]
        if element_type == "switch": continue
        net["res_" + element_type] = net["res_" + element_type].fillna(0)
        results[uuid] = {}
        results[uuid]["etype"] = element_type
        index = pp.get_element_index(net, element_type, uuid, exact_match=True)
        results[uuid].update(net["res_" + element_type].iloc[index].to_dict())

    message["elements"] = results
    return json.dumps(message)


def powerflow(data):
    elements_dict = data["elements"]
    buses = {}

    def process_potential_bus(key, value):
        if value in buses.keys():
            return buses[value]
        else:
            return value

    bus_list = [(uuid, element) for uuid, element in elements_dict.items() if element["etype"]=="bus"]
    element_list = [(uuid, element) for uuid, element in elements_dict.items() if element["etype"]!="bus"]

    net = pp.create_empty_network()

    for uuid, bus in bus_list:
        element_type = "bus"
        req_props = utils.required_props[element_type]
        positional_args = [value for key, value in bus.items() if key in req_props]
        optional_args = {key:value for key, value in bus.items() if (not key in req_props) and (not key == "etype")}
        index = pp.create_bus(net, *positional_args, **optional_args, name=uuid)
        buses[uuid] = index

    for uuid, element in element_list:
        element_type = utils.get_or_error("etype", element)
        req_props = utils.required_props[element_type]
        positional_args = [process_potential_bus(key, value) for key, value in element.items() if key in req_props]
        optional_args = {key:value for key, value in element.items() if (not key in req_props) and (not key == "etype")}

        if element_type == "load":
            pp.create_load(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "gen":
            pp.create_gen(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "ext_grid":
            pp.create_ext_grid(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "line":
            pp.create_line(net, *positional_args, *optional_args, name=uuid)
        elif element_type == "trafo":
            pp.create_transformer(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "storage":
            pp.create_storage(net, *positional_args, **optional_args, name=uuid)
        else:
            raise InvalidError(f"Element type {element_type} is invalid or not implemented!")
            
    try:
        pp.runpp(net)
    except LoadflowNotConverged:
        report = pp.diagnostic(net, report_style="compact", warnings_only=True)
        raise ConvError("Load flow did not converge.")
    except (KeyError, ValueError) as e:
        raise PPError(str(e))
    except Exception as e:
        raise PPError("Unknown exception has occured: " + str(e))

    message = {}
    message["status"] = "POWERFLOW_RESULT"
    results = {}
    for uuid, element in elements_dict.items():
        element_type = elements_dict[uuid]["etype"]
        net["res_"+element_type] = net["res_"+element_type].fillna(0)
        results[uuid] = {}
        results[uuid]["etype"] = element_type
        index = pp.get_element_index(net, element_type, uuid, exact_match=False)
        results[uuid].update(net["res_"+element_type].iloc[index].to_dict())
    message["elements"] = results
    return json.dumps(message)


def estimation(data):
    elements_dict = data["elements"]
    buses = {}

    def process_potential_bus(key, value):
        if value in buses.keys():
            return buses[value]
        else:
            return value

    bus_list = [(uuid, element) for uuid, element in elements_dict.items() if element["etype"]=="bus"]
    element_list = [(uuid, element) for uuid, element in elements_dict.items() if element["etype"]!="bus"]

    net = pp.create_empty_network()

    for uuid, bus in bus_list:
        element_type = "bus"
        req_props = utils.required_props[element_type]
        positional_args = [value for key, value in bus.items() if key in req_props]
        optional_args = {key:value for key, value in bus.items() if (not key in req_props) and (not key == "etype")}
        index = pp.create_bus(net, *positional_args, **optional_args, name=uuid)
        buses[uuid] = index

    for uuid, element in element_list:
        element_type = utils.get_or_error("etype", element)
        req_props = utils.required_props[element_type]
        positional_args = [process_potential_bus(key, value) for key, value in element.items() if key in req_props]
        optional_args = {key:value for key, value in element.items() if (not key in req_props) and (not key == "etype")}
        if element_type == "load":
            pp.create_load(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "gen":
            pp.create_gen(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "ext_grid":
            pp.create_ext_grid(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "line":
            pp.create_line(net, *positional_args, *optional_args, name=uuid)
        elif element_type == "trafo":
            pp.create_transformer(net, *positional_args, **optional_args, name=uuid)
        elif element_type == "storage":
            pp.create_storage(net, *positional_args, **optional_args, name=uuid)
        else:
            raise InvalidError(f"Element type {element_type} is invalid or not implemented!")



    measurements_dict = data["measurements"]

    measure_list = [(uuid, measure) for uuid, measure in measurements_dict.items()]
    for uuid, measure in measure_list:
        req_mes = utils.required_measures
        positional_args = [value for key, value in measure.items()]
        optional_args = {key:value for key, value in measure.items() if (not key in req_mes)}
        pp.create_measurement(net, *positional_args, **optional_args, name=uuid)


    try:
        est.estimate(net)
    except LoadflowNotConverged:
        report = pp.diagnostic(net, report_style="compact", warnings_only=True)
        raise ConvError("Load flow did not converge.")
    except (KeyError, ValueError) as e:
        raise PPError(str(e))
    except Exception as e:
        raise PPError("Unknown exception has occured: " + str(e))


    message = {}
    message["status"] = "EST_RESULT"
    results = {}
    for uuid, measure in measurements_dict.items():
        measure_type = measurements_dict[uuid]["element_type"]
        net["res_"+measure_type+"_est"] = net["res_"+measure_type+"_est"].fillna(0)
        results[uuid] = {}
        results[uuid]["element_type"] = measure_type
        results[uuid].update(net["res_"+measure_type+"_est"].iloc[0].to_dict())
        results[uuid].update(net["res_"+measure_type+"_est"].iloc[1].to_dict())
    message["measures"] = results
    return json.dumps(message)





# PROGRAM MAIN ENTRY POINT


if __name__ == "__main__":
    """ Entry point for program
    Just calls run and starts listening for requests
    """
    load_dotenv()
    host_addr = os.getenv("EBPP_HOST", "0.0.0.0")
    host_port = os.getenv("EBPP_PORT", "1127")
    debug_flag = False
    argc = len(sys.argv)
    if argc == 1:
        print("No arguments passed. Using defaults.")
    elif argc == 2:
        if sys.argv[1] == "-d":
            print("Running flask in debug mode.")
            host_addr = "127.0.0.1"
            debug_flag = True
        else:
            print(f"The flag {sys.argv[1]} is not a valid flag.")
    else:
        print("Invalid number of arguments given.")
    app.run(host=host_addr, port=host_port, debug=debug_flag)



