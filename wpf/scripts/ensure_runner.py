"""PR을 만든 뒤 self-hosted CI 실행기가 켜져 있는지 확인하고, 꺼져 있으면 Windows 로그온 작업으로 켠다.

플러그인 PostToolUse(Bash) 훅이 부른다(hooks/hooks.json). 훅 입력(stdin JSON)의 명령에 `gh pr create`가 없거나,
저장소 .agent-loop/config.json에 runner.task가 없으면 아무것도 하지 않는다. 직접 확인할 때: python -I ensure_runner.py --now
결과는 훅 JSON(systemMessage·additionalContext)으로 사용자와 에이전트에게 알린다. 실패해도 막지 않는다.
"""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LISTENER = "Runner.Listener.exe"
WAIT_S = 60


def runners():
    done = subprocess.run(["gh", "api", "repos/{owner}/{repo}/actions/runners", "--jq", ".runners"],
                          capture_output=True, text=True, encoding="utf-8")
    return json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None


def listener_running():
    # tasklist 안내문은 콘솔 코드 페이지(cp949)라 UTF-8로 풀면 예외가 난다. 이름은 ASCII라 바이트로 찾는다.
    done = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {LISTENER}", "/NH"], capture_output=True)
    return LISTENER.lower().encode() in done.stdout.lower()


def local_online(listed, host):
    return any(r["name"].lower() == host.lower() and r["status"] == "online" for r in listed)


def should_start(listed, host, running):
    # 이 PC 실행기만 본다. 다른 PC 실행기가 offline이라고 켜면 두 번째 Listener가 기존 Listener를 멈춘다.
    return not running and not local_online(listed, host)


def busy(listed, host):
    return any(r["name"].lower() == host.lower() and r.get("busy") for r in listed)


def local_busy():
    """이 PC의 CI 실행기가 지금 작업 중인가. 같은 PC에서 Unity를 함께 띄우면 CI와 충돌한다. 상태를 못 읽으면 False(막지 않는다)."""
    return busy(runners() or [], socket.gethostname())


def describe(listed):
    return ", ".join(f"{r['name']} {r['status']}{' (작업 중)' if r.get('busy') else ''}" for r in listed) or "등록된 실행기 없음"


def runner_task():
    """설정의 실행기 로그온 작업 이름. 루프 설정이 없는 저장소나 실행기를 쓰지 않는 저장소면 None."""
    try:
        from loopconfig import CONFIG
    except SystemExit:
        return None
    return (CONFIG.get("runner") or {}).get("task")


def check(task):
    listed = runners()
    if listed is None:
        return "CI 실행기 상태를 읽지 못했습니다(gh api 실패). 직접 확인하세요."
    host = socket.gethostname()
    if not should_start(listed, host, listener_running()):
        return f"CI 실행기 켜져 있음: {describe(listed)}"
    started = subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True)
    if started.returncode != 0:
        return f"CI 실행기가 꺼져 있고({describe(listed)}) 로그온 작업 {task}를 실행하지 못했습니다. 직접 켜세요."
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        time.sleep(5)
        listed = runners() or []
        if local_online(listed, host):
            return f"CI 실행기가 꺼져 있어 켰습니다: {describe(listed)}"
    return f"CI 실행기를 켜려 했지만 {WAIT_S}초 안에 online이 되지 않았습니다: {describe(listed)}. 직접 확인하세요."


def main(argv):
    if "--now" not in argv:
        try:
            command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
        except ValueError:
            return 0
        if "gh pr create" not in command:
            return 0
    task = runner_task()
    if not task:
        return 0
    message = check(task)
    print(json.dumps({"systemMessage": message,
                      "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
