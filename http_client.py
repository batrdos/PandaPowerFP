import requests


url = "http://127.0.0.1:5000/api/POW"
output = requests.get(url)

print(output)