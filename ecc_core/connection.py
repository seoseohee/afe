"""
ecc_core/connection.py

Claude Code에서 Bash 도구는 '로컬 셸에서 명령을 실행한다'는 단순한 전제 위에 있다.
임베디드 환경에서는 그 전제가 무너진다:
  - 물리 케이블 / Wi-Fi 불안정으로 연결이 끊긴다
  - 보드가 재부팅되면 프로세스 상태가 사라진다
  - 타임아웃은 '오래 걸림'이 아니라 '하드웨어 멈춤'을 의미할 수 있다

이 모듈은 그 불안정한 채널을 추상화해서
상위 레이어(도구, 에이전트)에게 '안정적인 실행 인터페이스'를 제공한다.
"""

import subprocess
import time
import os
import ipaddress
import socket
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────
# 실행 결과 표준 포맷
# Claude Code의 tool_result와 1:1 대응
# ─────────────────────────────────────────────────────────────

@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    rc: int
    duration_ms: int = 0

    def output(self) -> str:
        """stdout + stderr 통합 출력 (LLM에게 돌려줄 텍스트)"""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr}")
        if not parts:
            return f"(no output, rc={self.rc})"
        return "\n".join(parts)

    def to_tool_result(self) -> str:
        """LLM의 tool_result content로 직렬화"""
        status = "ok" if self.ok else f"error(rc={self.rc})"
        return f"[{status}] {self.duration_ms}ms\n{self.output()}"


# ─────────────────────────────────────────────────────────────
# SSH 연결 — 임베디드 보드로의 유일한 통로
# ─────────────────────────────────────────────────────────────

class BoardConnection:
    """
    임베디드 보드와의 SSH 연결을 관리한다.

    Claude Code와의 차이:
      로컬 Bash → SSH 채널
      즉시 실행 → 연결 확인 후 실행
      에러 = 프로그램 버그 → 에러 = 물리/네트워크 문제일 수 있음

    설계 원칙:
      - 연결 실패는 예외가 아니라 정상적인 상태값으로 처리
      - 스크립트는 인라인 heredoc 대신 파일 업로드 방식 사용
        (이유: 서브셸 환경변수 소실, 따옴표 이스케이프 지옥 방지)
      - 재연결 로직은 여기서만 처리 (상위 레이어는 신경 안 써도 됨)
    """

    SSH_OPTS = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=5",   # 5초마다 keepalive
        "-o", "ServerAliveCountMax=3",   # 3번 무응답 시 끊김 감지
    ]

    def __init__(self, host: str, user: str = "root", port: int = 22):
        self.host = host
        self.user = user
        self.port = port
        self._consecutive_failures = 0

    @property
    def address(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    def run(self, cmd: str, timeout: int = 30) -> ExecResult:
        """단일 명령 실행"""
        full_cmd = (
            ["ssh"] + self.SSH_OPTS
            + ["-p", str(self.port), f"{self.user}@{self.host}", cmd]
        )
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                full_cmd, capture_output=True, text=True, timeout=timeout
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            result = ExecResult(
                ok=r.returncode == 0,
                stdout=r.stdout,
                stderr=r.stderr,
                rc=r.returncode,
                duration_ms=elapsed,
            )
            self._consecutive_failures = 0 if result.ok else self._consecutive_failures + 1
            return result
        except subprocess.TimeoutExpired:
            elapsed = int((time.monotonic() - t0) * 1000)
            self._consecutive_failures += 1
            return ExecResult(
                ok=False, stdout="", stderr=f"timeout after {timeout}s",
                rc=-1, duration_ms=elapsed
            )
        except Exception as e:
            self._consecutive_failures += 1
            return ExecResult(ok=False, stdout="", stderr=str(e), rc=-1)

    def upload_and_run(
        self, script: str, interpreter: str = "bash", timeout: int = 60
    ) -> ExecResult:
        """
        스크립트를 파일로 업로드한 뒤 실행.

        인라인 heredoc 대신 이 방식을 사용하는 이유:
          1. ROS2 'source setup.bash' 같은 환경변수 체인이 서브셸에서 사라지지 않음
          2. 복잡한 따옴표/특수문자 이스케이프 불필요
          3. 스크립트 길이 제한 없음

        세션 로그에서 확인된 패턴: 인라인 명령의 환경변수 소실이
        F1Tenth 제어 실패 원인 중 하나였음
        """
        ts = int(time.time() * 1000)
        remote_path = f"/tmp/_ecc_{ts}"

        # SCP 업로드
        upload_cmd = (
            ["scp"] + self.SSH_OPTS
            + ["-P", str(self.port), "/dev/stdin",
               f"{self.user}@{self.host}:{remote_path}"]
        )
        try:
            subprocess.run(
                upload_cmd, input=script.encode(),
                capture_output=True, timeout=15
            )
        except Exception as e:
            return ExecResult(ok=False, stdout="", stderr=f"upload failed: {e}", rc=-1)

        # 실행 후 즉시 삭제 (보드 /tmp 용량 절약)
        return self.run(
            f"{interpreter} {remote_path}; _rc=$?; rm -f {remote_path}; exit $_rc",
            timeout=timeout
        )

    def is_alive(self) -> bool:
        """연결 상태 확인 — 10 turn마다 호출"""
        r = self.run("echo __ecc_ping__", timeout=6)
        return r.ok and "__ecc_ping__" in r.stdout

    def reconnect(self, max_attempts: int = 3) -> bool:
        """지수 백오프로 재연결 시도"""
        for attempt in range(max_attempts):
            wait = 2 ** attempt  # 1s, 2s, 4s
            time.sleep(wait)
            if self.is_alive():
                self._consecutive_failures = 0
                return True
        return False

    @property
    def likely_disconnected(self) -> bool:
        """연속 실패 3회 이상이면 연결 문제로 판단"""
        return self._consecutive_failures >= 3


# ─────────────────────────────────────────────────────────────
# 보드 탐색 — Claude Code에 없는 임베디드 전용 기능
# ─────────────────────────────────────────────────────────────

class BoardDiscovery:
    """
    네트워크에서 임베디드 보드를 찾는다.

    Claude Code는 '이미 로컬에 있다'고 전제하지만,
    임베디드 보드는 찾는 것 자체가 첫 번째 과제다.
    DHCP로 IP가 바뀌기도 하고, mDNS가 안 될 수도 있다.

    탐색 전략 (세션 로그에서 실패 패턴을 학습한 순서):
      1. known_hosts — 이전에 연결했던 IP (가장 빠름)
      2. mDNS — .local 도메인 (Raspberry Pi 등)
      3. 환경변수 힌트 — ECC_BOARD_HOST
      4. 서브넷 병렬 스캔 — 마지막 수단이지만 가장 확실
    """

    DEFAULT_USERS = ["root", "ubuntu", "jetson", "pi", "admin", "debian", "user"]

    @classmethod
    def from_hint(cls, host: str, user: Optional[str], port: int) -> Optional[BoardConnection]:
        """직접 지정된 경우 빠르게 확인"""
        users = [user] if user else cls.DEFAULT_USERS
        for u in users:
            conn = BoardConnection(host, u, port)
            if conn.is_alive():
                return conn
        return None

    @classmethod
    def scan(cls, user: Optional[str] = None, port: int = 22) -> Optional[BoardConnection]:
        """네트워크 자동 탐색"""
        candidates: list[str] = []
        users = [user] if user else cls.DEFAULT_USERS

        # 1. 환경변수
        env_host = os.environ.get("ECC_BOARD_HOST")
        if env_host:
            candidates.append(env_host)

        # 2. known_hosts (이전 세션에서 연결했던 IP)
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if os.path.exists(known_hosts):
            with open(known_hosts) as f:
                for line in f:
                    h = line.split()[0].split(",")[0] if line.strip() else ""
                    try:
                        ipaddress.ip_address(h)
                        if h not in candidates:
                            candidates.append(h)
                    except ValueError:
                        pass

        # 3. mDNS
        for mdns_name in [
            "jetson.local", "raspberrypi.local", "rpi.local",
            "ubuntu.local", "embedded.local", "board.local",
        ]:
            try:
                ip = socket.gethostbyname(mdns_name)
                if ip not in candidates:
                    candidates.append(ip)
            except Exception:
                pass

        # 4. 서브넷 스캔 (병렬 ping)
        subnet_ips = cls._get_subnet_ips()
        if subnet_ips:
            print(f"  🌐 {len(subnet_ips)}개 IP 병렬 스캔...", flush=True)
            with ThreadPoolExecutor(max_workers=200) as executor:
                futures = {executor.submit(cls._ping, ip): ip for ip in subnet_ips}
                for future in as_completed(futures):
                    ip = future.result()
                    if ip and ip not in candidates:
                        candidates.append(ip)

        # SSH 연결 확인
        print(f"  🔑 {len(candidates)}개 후보 SSH 확인...", flush=True)
        for ip in candidates:
            for u in users:
                conn = BoardConnection(ip, u, port)
                if conn.is_alive():
                    return conn
        return None

    @staticmethod
    def _ping(ip: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", "1", ip],
                capture_output=True, timeout=2
            )
            return ip if r.returncode == 0 else None
        except Exception:
            return None

    @staticmethod
    def _get_subnet_ips() -> list[str]:
        """로컬 라우팅 테이블에서 서브넷 추출"""
        ips: list[str] = []
        try:
            r = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                parts = line.split()
                if parts and "/" in parts[0] and "via" not in line:
                    try:
                        net = ipaddress.ip_network(parts[0], strict=False)
                        if 16 <= net.prefixlen <= 24:
                            base = str(net.network_address).rsplit(".", 1)[0]
                            ips += [f"{base}.{i}" for i in range(1, 255)]
                    except ValueError:
                        pass
        except Exception:
            pass

        if not ips:  # fallback
            for base in ["192.168.1", "192.168.0", "10.0.0", "10.42.0"]:
                ips += [f"{base}.{i}" for i in range(1, 255)]
        return ips
