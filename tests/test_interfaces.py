import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_task_board.cli import main
from agent_task_board.config import make_board
from agent_task_board.mcp_server import build_server
from agent_task_board.web.server import make_server


def test_cli_json_parameters_errors_and_version(tmp_path, capsys):
    base = ["--data-dir", str(tmp_path)]
    assert main(["add", "Example", "--json", *base, "--dedupe", "example"]) == 0
    task = json.loads(capsys.readouterr().out)["data"]
    assert main([*base, "show", task["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert main([*base, "show", "missing"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    assert main([*base, "claim"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert main(["--version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["version"] == "0.1.0"
    assert main([*base, "add", "bad", "--params", "not-json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_config_precedence(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{tmp_path / "config-data"}"\nlease_seconds=12\n')
    assert make_board(config=str(config)).lease_seconds == 12
    monkeypatch.setenv("ATB_DATA_DIR", str(tmp_path / "env"))
    assert make_board(config=str(config)).data_dir == tmp_path / "env"
    assert make_board(str(tmp_path / "cli"), str(config)).data_dir == tmp_path / "cli"


def test_every_mcp_tool_via_official_sdk(board):
    mcp = build_server(board)

    async def call(name, arguments):
        result = await mcp.call_tool(name, arguments)
        if isinstance(result, tuple):
            return result[1]
        if isinstance(result, dict):
            return result
        return json.loads(result[0].text)

    async def run():
        names = {tool.name for tool in await mcp.list_tools()}
        assert names == {
            "create_task",
            "claim_task",
            "heartbeat",
            "complete_task",
            "fail_task",
            "list_tasks",
            "add_comment",
            "get_task",
            "cancel_task",
        }
        created = (await call("create_task", {"title": "MCP example", "max_attempts": 1}))["data"]
        assert (await call("get_task", {"id": created["id"]}))["ok"]
        assert (await call("list_tasks", {"filters": {"queue": "default"}}))["data"][0][
            "id"
        ] == created["id"]
        assert not (await call("list_tasks", {"filters": {"unknown": True}}))["ok"]
        assert (await call("add_comment", {"id": created["id"], "text": "Prepared"}))["ok"]
        task = (await call("claim_task", {"agent": "worker"}))["data"]
        lease = {"id": task["id"], "agent": "worker", "token": task["lease_token"]}
        assert (await call("heartbeat", lease))["data"]["status"] == "running"
        assert (await call("fail_task", {**lease, "error": "Example failure"}))["data"][
            "status"
        ] == "failed"
        second = (await call("create_task", {"title": "Completion"}))["data"]
        task = (await call("claim_task", {"agent": "worker"}))["data"]
        lease = {"id": second["id"], "agent": "worker", "token": task["lease_token"]}
        assert (await call("complete_task", {**lease, "result": {"answer": 3}}))["data"][
            "status"
        ] == "done"
        third = (await call("create_task", {"title": "Cancel"}))["data"]
        assert not (await call("cancel_task", {"id": third["id"]}))["ok"]
        assert (await call("cancel_task", {"id": third["id"], "confirm": True}))["data"][
            "status"
        ] == "cancelled"
        assert not (await call("get_task", {"id": "missing"}))["ok"]

    asyncio.run(run())


@pytest.fixture
def web(board):
    servers = []

    def start(read_only=False):
        server = make_server(board, port=0, read_only=read_only, quiet=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()


def request(url, payload=None, token=None):
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-ATB-CSRF"] = token
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def test_web_read_only_rejects_mutations(web, board):
    board.create_task("Read-only example")
    url = web(True)
    state = request(url + "/api/state")["data"]
    assert state["read_only"] and state["csrf"] is None
    assert len(state["tasks"]) == 1
    with pytest.raises(urllib.error.HTTPError) as error:
        request(url + "/api/tasks", {"title": "Forbidden"})
    assert error.value.code == 403
    assert len(board.list_tasks()) == 1


def test_web_same_origin_mutation_and_review(web, board):
    url = web()
    token = request(url + "/api/state")["data"]["csrf"]
    with pytest.raises(urllib.error.HTTPError):
        request(url + "/api/tasks", {"title": "No token"})
    task = request(url + "/api/tasks", {"title": "Review example", "needs_approval": True}, token)[
        "data"
    ]
    claimed = board.claim("w")
    board.complete(task["id"], "w", claimed["lease_token"])
    assert (
        request(url + "/api/review", {"id": task["id"], "approved": True}, token)["data"]["status"]
        == "done"
    )
    assert request(url + "/api/comment", {"id": task["id"], "text": "Accepted"}, token)["ok"]
    assert request(url + "/api/events?task=" + task["id"])["data"][-1]["type"] == "comment"
    with urllib.request.urlopen(url + "/") as response:
        assert b"Agent Task Board" in response.read()
    with pytest.raises(urllib.error.HTTPError) as error:
        request(url + "/api/review", {"id": task["id"], "approved": "false"}, token)
    assert error.value.code == 400
