import json
import os
from pathlib import Path
from antithesis import lifecycle
from fastapi import FastAPI, Depends
import uvicorn

class Workload:
    def __init__(self):
        print("Starting workload")
        outfile = Path(os.environ.get("ANTITHESIS_OUTPUT_DIR", "")) / "sdk.jsonl"
        m = {"antithesis_setup": { "status": "complete", "details": {"message": "Set up complete - ready for testing!" }}}
        with outfile.open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(m))
        accounts_json = Path("/accounts.json")
        if accounts_json.is_file():
            self.account_data = json.loads(Path("/accounts.json").read_text())
            print(json.dumps(self.account_data, indent=2))
        else:
            print(f"Can't load account data from {accounts_json.resolve()}")

    async def pay(self):
        print("hit pay")
        return {"cool": "beans"}


def create_app(workload: Workload) -> FastAPI:
    app = FastAPI()

    def get_workload():
        return workload

    @app.get("/pay")
    async def make_payment(w: Workload = Depends(get_workload)):
        return await w.pay()

    return app

def main():
    outfile = Path(os.environ.get("ANTITHESIS_OUTPUT_DIR", "")) / "sdk.jsonl"
    m = {"antithesis_setup": { "status": "complete", "details": {"message": "Set up complete - ready for testing!" }}}
    print(json.dumps(m, indent=2))
    print(f"Writing\n\t{m}\nto\n\t{outfile}")
    lifecycle.setup_complete(details={"message": "Workload initialization complete"})
    with outfile.open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(m))
    while True:
        workload = Workload()
        app = create_app(workload)
        uvicorn.run(app, host="0.0.0.0", port=8000)
