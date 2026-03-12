"""
ecc_core/connection.py

환경변수:
  ECC_BOARD_HOST   보드 IP 힌트
  ECC_USERS        SSH 사용자 목록, 쉼표 구분 (기본: root,ubuntu,jetson,pi,admin,debian,user)
  ECC_MDNS         mDNS 이름 목록, 쉼표 구분
  ECC_SUBNETS      fallback 서브넷, 쉼표 구분 (기본: 192.168.1,192.168.0,10.0.0,10.42.0)
  ECC_SCAN_WORKERS 병렬 ping 스레드 수 (기본: 200)
  ECC_SSH_TIMEOUT  SSH ConnectTimeout 초 (기본: 10)
"""

import platform
import subprocess
import time
import os
import ipaddress
import socket
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


def _env_list(key: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(key, default).split(",") if v.strip()]

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    rc: int
    duration_ms: int = 0

    def output(self) -> str:
        parts = []
        stdout = self.stdout or ""
        stderr = self.stderr or ""
        if stdout.strip():
            parts.append(stdout)
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr}")
        if not parts:
            return f"(no output, rc={self.rc})"
        return "\n".join(parts)

    def filtered_output(self) -> str:
        """버그9: ros2 topic pub --rate/--times의 verbose 메시지 필터링.

        'publishing #N: ...' 라인은 대부분 중복 노이즈.
        첫 2줄만 남기고 나머지는 요약으로 대체.
        다른 유용한 라인(data:, linear:, x:, ERPM 등)은 보존.
        """
        out = self.output()
        lines = out.splitlines()

        pub_lines = []
        other_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("publishing #") or stripped.startswith("publisher:"):
                pub_lines.append(line)
            else:
                other_lines.append(line)

        if len(pub_lines) > 2:
            # 첫 줄만 샘플로 남기고 요약
            result_lines = other_lines + [
                pub_lines[0],
                f"... [{len(pub_lines)} publish messages total, showing first only] ...",
            ]
            return "\n".join(result_lines)

        return out

    def to_tool_result(self, max_chars: int = 4000) -> str:
        status = "ok" if self.ok else f"error(rc={self.rc})"
        out = self.filtered_output()  # 버그9: verbose 필터 적용
        if len(out) > max_chars:
            head = out[:max_chars // 2]
            tail = out[-(max_chars // 4):]
            out = f"{head}\n...[{len(out)} chars truncated, showing head+tail]...\n{tail}"
        return f"[{status}] {self.duration_ms}ms\n{out}"


class BoardConnection:

    @property
    def SSH_OPTS(self) -> list[str]:
        timeout = _env_int("ECC_SSH_TIMEOUT", 10)
        return [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"ConnectTimeout={timeout}",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
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
        full_cmd = (
            ["ssh"] + self.SSH_OPTS
            + ["-p", str(self.port), f"{self.user}@{self.host}", cmd]
        )
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                full_cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout
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
            # 버그6: timeout 시 원격 고아 프로세스 정리
            # SSH 세션은 이미 끊겼으므로 별도 연결로 cleanup
            self._kill_remote_orphans(cmd)
            return ExecResult(ok=False, stdout="", stderr=f"timeout after {timeout}s", rc=-1, duration_ms=elapsed)
        except Exception as e:
            self._consecutive_failures += 1
            return ExecResult(ok=False, stdout="", stderr=str(e), rc=-1)

    def _kill_remote_orphans(self, cmd: str) -> None:
        """timeout된 SSH 명령의 원격 프로세스를 정리한다.

        원격에서 실행 중인 _ecc_ 임시 스크립트와 ros2 pub 프로세스를
        새 SSH 연결로 kill. 실패해도 조용히 무시 (best-effort).
        """
        try:
            # /tmp/_ecc_* 스크립트와 자식 프로세스 정리
            cleanup = (
                "pkill -f '/tmp/_ecc_' 2>/dev/null; "
                "pkill -f 'ros2 topic pub' 2>/dev/null; "
                "rm -f /tmp/_ecc_* 2>/dev/null; "
                "true"
            )
            kill_cmd = (
                ["ssh"] + self.SSH_OPTS
                + ["-p", str(self.port), f"{self.user}@{self.host}", cleanup]
            )
            subprocess.run(kill_cmd, capture_output=True, timeout=8)
        except Exception:
            pass  # cleanup 실패는 무시

    def upload_and_run(self, script: str, interpreter: str = "bash", timeout: int = 60) -> ExecResult:
        """스크립트를 base64 청크로 원격에 쓰고 실행한다. scp/ARG_MAX 문제 없음."""
        import base64 as _b64
        ts = int(time.time() * 1000)
        remote_path = f"/tmp/_ecc_{ts}"

        b64 = _b64.b64encode(script.encode("utf-8")).decode("ascii")

        # 긴 스크립트는 청크로 나눠 전송 (ARG_MAX ~2MB 회피, 안전 기준 4000자)
        CHUNK = 4000
        chunks = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)]

        # 버그8: write timeout을 스크립트 크기에 비례해 자동 설정 (최소 30s)
        write_timeout = max(30, len(chunks) * 5)

        if len(chunks) == 1:
            write_cmd = f"printf '%s' {chunks[0]} | base64 -d > {remote_path}"
            r = self.run(write_cmd, timeout=write_timeout)
        else:
            lines = []
            for i, chunk in enumerate(chunks):
                op = ">" if i == 0 else ">>"
                lines.append(f"printf '%s' {chunk} | base64 -d {op} {remote_path}.b64")
            lines.append(f"base64 -d {remote_path}.b64 > {remote_path} && rm -f {remote_path}.b64")
            write_cmd = " && ".join(lines)
            r = self.run(write_cmd, timeout=write_timeout)

        if not r.ok:
            return ExecResult(ok=False, stdout="", stderr=f"script write failed (rc={r.rc}): {r.stderr}", rc=-1)

        return self.run(
            f"{interpreter} {remote_path}; _rc=$?; rm -f {remote_path}; exit $_rc",
            timeout=timeout,
        )

    def is_alive(self) -> bool:
        r = self.run("echo __ecc_ping__", timeout=6)
        return r.ok and "__ecc_ping__" in r.stdout

    def reconnect(self, max_attempts: int = 3) -> bool:
        for attempt in range(max_attempts):
            time.sleep(2 ** attempt)
            if self.is_alive():
                self._consecutive_failures = 0
                return True
        return False

    @property
    def likely_disconnected(self) -> bool:
        return self._consecutive_failures >= 3


class BoardDiscovery:

    @classmethod
    def _default_users(cls) -> list[str]:
        return _env_list("ECC_USERS", "root,ubuntu,jetson,pi,admin,debian,user")

    @classmethod
    def _default_mdns(cls) -> list[str]:
        return _env_list(
            "ECC_MDNS",
            "jetson.local,raspberrypi.local,rpi.local,ubuntu.local,embedded.local,board.local"
        )

    @classmethod
    def _default_subnets(cls) -> list[str]:
        return _env_list("ECC_SUBNETS", "192.168.1,192.168.0,10.0.0,10.42.0")

    # loop.py에서 BoardDiscovery.DEFAULT_USERS 직접 참조 호환성 유지
    DEFAULT_USERS = property(lambda self: self._default_users())

    @classmethod
    def from_hint(cls, host: str, user: Optional[str], port: int) -> Optional[BoardConnection]:
        users = [user] if user else cls._default_users()
        for u in users:
            conn = BoardConnection(host, u, port)
            if conn.is_alive():
                return conn
        return None

    @classmethod
    def scan(cls, user: Optional[str] = None, port: int = 22) -> Optional[BoardConnection]:
        candidates: list[str] = []
        users = [user] if user else cls._default_users()

        env_host = os.environ.get("ECC_BOARD_HOST")
        if env_host:
            candidates.append(env_host)

        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if os.path.exists(known_hosts):
            with open(known_hosts, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # hashed 형식 (|1|...|...) 은 IP 추출 불가 — 스킵
                    if line.startswith("|"):
                        continue
                    h = line.split()[0].split(",")[0]
                    # [IP]:port 형식 처리
                    if h.startswith("["):
                        h = h[1:].split("]")[0]
                    try:
                        ipaddress.ip_address(h)
                        if h not in candidates:
                            candidates.append(h)
                    except ValueError:
                        pass

        for mdns_name in cls._default_mdns():
            try:
                ip = socket.gethostbyname(mdns_name)
                if ip not in candidates:
                    candidates.append(ip)
            except Exception:
                pass

        subnet_ips = cls._get_subnet_ips()
        if subnet_ips:
            workers = _env_int("ECC_SCAN_WORKERS", 200)
            print(f"  🌐 {len(subnet_ips)}개 IP 병렬 스캔 (workers={workers})...", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(cls._ping, ip): ip for ip in subnet_ips}
                for future in as_completed(futures):
                    ip = future.result()
                    if ip and ip not in candidates:
                        candidates.append(ip)

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
            if platform.system() == "Windows":
                cmd = ["ping", "-n", "1", "-w", "1000", ip]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", ip]
            r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=3)
            return ip if r.returncode == 0 else None
        except Exception:
            return None

    @classmethod
    def _get_subnet_ips(cls) -> list[str]:
        ips: list[str] = []
        try:
            if platform.system() == "Windows":
                r = subprocess.run(["ipconfig"], capture_output=True, encoding="utf-8", errors="replace", timeout=5)
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if "IPv4" in line or "IP Address" in line:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            ip = parts[-1].strip()
                            try:
                                ipaddress.ip_address(ip)
                                if not ip.startswith("127."):
                                    base = ip.rsplit(".", 1)[0]
                                    ips += [f"{base}.{i}" for i in range(1, 255)]
                            except ValueError:
                                pass
            else:
                r = subprocess.run(["ip", "route"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=5)
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

        if not ips:
            for base in cls._default_subnets():
                ips += [f"{base}.{i}" for i in range(1, 255)]
        else:
            # NIC 기반 스캔 결과에 fallback 서브넷도 항상 추가
            # (보드가 PC와 다른 서브넷에 있을 수 있음)
            existing_bases = {ip.rsplit(".", 1)[0] for ip in ips}
            for base in cls._default_subnets():
                if base not in existing_bases:
                    ips += [f"{base}.{i}" for i in range(1, 255)]
        return ips
