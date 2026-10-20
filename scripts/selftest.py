#!/usr/bin/env python3
"""链路自检：不需要第二台机器，也不需要显示器就能验证 QuickSwitch 的通信链路。

做法：
  1. 用临时配置在本机启动一个真实的 QuickSwitch 进程（``--no-tray --dry-run``）；
  2. 本脚本作为「对端」与它完成 hello / 心跳 / switch / switch_ack 全流程；
  3. 再用一个「密钥不对」的连接尝试切换，验证它被拒绝；
  4. 检查守护进程日志中是否出现预期的切换记录。

用法：
  python3 scripts/selftest.py
  python3 scripts/selftest.py --python /usr/bin/python3     # 指定注入的 Python
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from network import PeerConnection  # noqa: E402  （需要 PROJECT_DIR 在 sys.path 中）

AUTH_SECRET = "selftest-secret-0123456789abcdef"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def base_config(role: str, listen_port: int, peer_port: int) -> Dict[str, Any]:
    return {
        "role": role,
        "peer_host": "127.0.0.1",
        "peer_port": peer_port,
        "listen_port": listen_port,
        "debounce": 0,
        "network": {"ping_interval": 1.0, "ack_timeout": 3.0,
                    "reconnect_interval": 0.3, "reconnect_max_interval": 1.0},
        "ddc": {"mac_input": 15, "win_input": 17, "timeout": 3.0, "retries": 0, "retry_delay": 0},
        "auth": {"secret": AUTH_SECRET},
    }


async def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def run(project_dir: Path, python: str, timeout: float) -> int:
    results: List[tuple] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        server_port, client_port = free_port(), free_port()
        server_cfg = tmp_path / "server.json"
        client_cfg = tmp_path / "client.json"
        server_cfg.write_text(
            json.dumps(base_config("server", server_port, client_port), ensure_ascii=False),
            encoding="utf-8",
        )
        client_cfg.write_text(
            json.dumps(base_config("client", client_port, server_port), ensure_ascii=False),
            encoding="utf-8",
        )
        log_path = tmp_path / "daemon.log"
        env = dict(
            os.environ,
            QUICKSWITCH_CONFIG=str(server_cfg),
            QUICKSWITCH_LOG=str(log_path),
        )

        print(f"启动被测进程：{python} main.py --no-tray --dry-run")
        print(f"  server 监听 {server_port}，peer 指向 {client_port}")
        process = subprocess.Popen(
            [python, str(project_dir / "main.py"), "--no-tray", "--dry-run"],
            cwd=str(project_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        acks: List[Dict[str, Any]] = []

        async def on_message(msg: Dict[str, Any]) -> None:
            if msg.get("type") == "switch_ack":
                acks.append(msg)

        client = PeerConnection(
            "127.0.0.1", server_port, client_port, on_message,
            name="selftest",
            identity={"name": "selftest", "role": "client", "version": "selftest"},
            ping_interval=1.0, reconnect_interval=0.3, reconnect_max_interval=1.0,
            secret=AUTH_SECRET,
        )
        try:
            await client.start()
            check("对端连接建立", await client.wait_connected(timeout), f"等待 {timeout:g}s")
            if not client.connected:
                return 1

            check("收到 hello（身份交换）", await wait_for(lambda: bool(client.peer_info), 5.0),
                  str(client.peer_info.get("name", "")))
            check("密钥校验通过（两端 auth.secret 一致）",
                  await wait_for(lambda: client.authenticated, 5.0))

            sent = await client.send({"type": "switch", "target": "mac", "id": "selftest-1"})
            check("切换请求已发出", sent)
            got = await wait_for(lambda: bool(acks), timeout)
            check("收到 switch_ack", got, json.dumps(acks[0], ensure_ascii=False) if acks else "")
            if got:
                check("对端 DDC 执行成功（dry-run）", bool(acks[0].get("ok")))

            # 反向：由被测进程主动请求本脚本切换
            inbound: List[Dict[str, Any]] = []

            async def on_inbound(msg: Dict[str, Any]) -> None:
                inbound.append(msg)
                if msg.get("type") == "switch":
                    await client.send(
                        {"type": "switch_ack", "id": msg.get("id"), "ok": True, "detail": "selftest"}
                    )

            client.on_message = on_inbound  # 模拟真实客户端的行为
            await client.send({"type": "ping", "ts": time.time()})
            check("心跳互通（收到 pong）",
                  await wait_for(lambda: any(m.get("type") == "pong" for m in inbound), 5.0))
        finally:
            await client.stop()

        # 密钥不对的连接：能连上、能收到 hello，但切换请求必须被丢弃
        forged_acks: List[Dict[str, Any]] = []

        async def on_forged(msg: Dict[str, Any]) -> None:
            forged_acks.append(msg)

        attacker = PeerConnection(
            "127.0.0.1", server_port, client_port, on_forged,
            name="selftest-wrong-key",
            identity={"name": "selftest-wrong-key", "role": "client"},
            ping_interval=1.0, reconnect_interval=0.3, reconnect_max_interval=1.0,
            secret="wrong-secret-0123456789abcdef",
        )
        try:
            await attacker.start()
            connected = await attacker.wait_connected(timeout)
            check("错误密钥的连接仍能建立（只拦消息，不拦连接）", connected)
            await attacker.send({"type": "switch", "target": "win", "id": "forged-1"})
            await asyncio.sleep(1.5)
            forged = [m for m in forged_acks if m.get("type") == "switch_ack"]
            check("错误密钥的切换没有得到 ACK", not forged,
                  json.dumps(forged[0], ensure_ascii=False) if forged else "")
        finally:
            await attacker.stop()

        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        output = process.stdout.read() if process.stdout else ""

        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        check("进程正常退出", process.returncode == 0, f"退出码 {process.returncode}")
        check("守护进程收到切换请求", "收到对端切换请求" in log_text)
        check("守护进程执行了本地 DDC", "本地 DDC → Mac：成功" in log_text)
        check("未通过校验的报文被丢弃", "丢弃未经校验的报文" in log_text)
        check("错误密钥没能触发切换", "本地 DDC → Windows：成功" not in log_text)
        check("日志写入指定文件", bool(log_text))
        if not log_text:
            print(output[-2000:])

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"自检失败：{len(failed)} 项 — {', '.join(failed)}")
        return 1
    print(f"自检通过：{len(results)} 项全部成功")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QuickSwitch 本机链路自检")
    parser.add_argument("--project-dir", default=str(PROJECT_DIR), help="QuickSwitch 目录")
    parser.add_argument("--python", default=sys.executable, help="运行 main.py 的解释器")
    parser.add_argument("--timeout", type=float, default=10.0, help="等待超时（秒）")
    args = parser.parse_args()
    return asyncio.run(run(Path(args.project_dir).resolve(), args.python, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
