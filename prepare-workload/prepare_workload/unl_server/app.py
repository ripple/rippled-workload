import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

unl_json = json.loads(Path("/unl.json").read_text(encoding="utf-8"))
address = "0.0.0.0"
port = 80


class Server(BaseHTTPRequestHandler):

    def do_GET(self):
        json_to_pass = json.dumps(unl_json)
        self.send_response(code=200)
        self.send_header(keyword="Content-type", value="application/json")
        self.end_headers()
        self.wfile.write(json_to_pass.encode("utf-8"))


def run(server_class=HTTPServer, handler_class=Server, addr=address, port=port):
    server_address = (addr, port)
    httpd = server_class(server_address, handler_class)
    print(f"UNL server on {addr}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
