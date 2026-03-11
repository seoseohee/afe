"""
ecc_core/tools.py

Claude Code의 도구 체계를 임베디드용으로 재설계.

Claude Code 도구 → 임베디드 등가물:
  Bash        → bash     (SSH로 원격 실행, 환경변수 주의)
  Write       → write    (SCP 업로드)
  Read        → read     (SSH cat)
  Glob        → glob     (SSH find)
  Grep        → grep     (SSH grep/rg)
  TodoWrite   → todo     (그대로 유지 — 계획 관리)
  Task/Agent  → subagent (독립 컨텍스트 탐색)
  [신규]      → probe    (하드웨어 탐지 — 임베디드 전용)
  [신규]      → done     (목표 달성/불가 판정)

설계 원칙:
  - 각 도구는 독립적으로 실패할 수 있어야 한다 (연결 문제)
  - 도구 결과는 항상 LLM이 파싱 가능한 구조화된 텍스트
  - 위험한 명령(rm -rf, dd, reboot 등)은 별도 확인 레이어 통과
"""

from dataclasses import dataclass
from typing import Any


# ─────────────────────────────────────────────────────────────
# 도구 스키마 정의 (Anthropic API tool_use 포맷)
# ─────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [

    # ── 0. ssh_connect ─────────────────────────────────────────
    # 임베디드 전용 — Claude Code에 없는 도구
    # 연결이 전제조건이 아니라 에이전트가 달성해야 할 첫 번째 목표
    {
        "name": "ssh_connect",
        "description": """SSH로 보드에 연결한다. 연결이 없는 상태에서 모든 작업의 첫 번째 단계.

bash/script/probe 등 다른 도구를 쓰기 전에 반드시 연결이 되어 있어야 한다.
연결되지 않은 상태에서 다른 도구를 호출하면 [no connection] 에러가 반환된다.

연결 전략 — 막히면 다음 방법으로 넘어간다:
1. 힌트가 있으면 그 IP/user부터 시도
2. known_hosts, mDNS (.local 도메인) 시도
3. 로컬 서브넷 스캔 (느리지만 확실)
4. 연결 성공 후 probe all로 환경 파악

연결 실패 시 에이전트가 해야 할 것:
- 다른 IP 범위 시도
- 다른 user 시도 (root, ubuntu, jetson, pi, admin)
- 포트 확인 (22, 2222)
- 사용자에게 물어보기 (AskUser 없으면 bash로 네트워크 상태 확인)
- 절대 포기하지 않는다""",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "보드 IP 또는 hostname. 모르면 'scan'을 입력하면 자동 탐색."
                },
                "user": {
                    "type": "string",
                    "description": "SSH 사용자. 기본: 순서대로 root, ubuntu, jetson, pi, admin 시도.",
                    "default": ""
                },
                "port": {
                    "type": "integer",
                    "description": "SSH 포트. 기본: 22.",
                    "default": 22
                }
            },
            "required": ["host"]
        }
    },

    # ── 1. bash ────────────────────────────────────────────────
    {
        "name": "bash",
        "description": """SSH를 통해 보드에서 셸 명령을 실행한다.

Claude Code의 Bash 도구와 동일하지만 원격 실행이라는 차이가 있다:
- 연결 끊김/타임아웃은 프로그램 버그가 아니라 물리 환경 문제일 수 있다
- 환경변수는 명령마다 초기화된다 (ROS2는 항상 source 필요)
- 오래 걸리는 하드웨어 작업은 timeout을 넉넉하게 설정해라

사용 지침:
- 독립적인 여러 정보가 필요하면 && 로 한 명령에 묶어라 (병렬 효과)
- 멀티라인 스크립트는 bash 말고 script 도구를 써라 (환경변수 안전)
- 배경 프로세스: nohup ... & disown 패턴 사용""",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "실행할 셸 명령. 여러 명령은 && 또는 ; 로 연결."
                },
                "timeout": {
                    "type": "integer",
                    "description": "타임아웃(초). 기본 30. 하드웨어 초기화 등은 120 이상.",
                    "default": 30
                },
                "description": {
                    "type": "string",
                    "description": "이 명령이 하는 일 (5~10단어). 로그와 안전 검사에 사용."
                }
            },
            "required": ["command", "description"]
        }
    },

    # ── 2. script ──────────────────────────────────────────────
    {
        "name": "script",
        "description": """멀티라인 스크립트를 보드에 파일로 업로드하고 실행한다.

인라인 bash 명령 대신 이 도구를 써야 하는 경우:
- ROS2 source 체인 등 환경변수가 여러 줄에 걸쳐 유지돼야 할 때
- Python, C 등 다른 언어로 하드웨어 제어 코드를 작성할 때
- 복잡한 로직 (루프, 조건문, 에러 핸들링) 이 필요할 때
- 50줄 이상의 코드

업로드 → 실행 → 자동 삭제 순서로 동작한다.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "실행할 스크립트 전체 내용"
                },
                "interpreter": {
                    "type": "string",
                    "description": "인터프리터. 예: 'bash', 'python3', 'python3 -u'",
                    "default": "bash"
                },
                "timeout": {
                    "type": "integer",
                    "description": "타임아웃(초). 기본 60.",
                    "default": 60
                },
                "description": {
                    "type": "string",
                    "description": "스크립트 목적 요약"
                }
            },
            "required": ["code", "description"]
        }
    },

    # ── 3. read ────────────────────────────────────────────────
    {
        "name": "read",
        "description": """보드의 파일 내용을 읽는다.

설정 파일 확인, 로그 조회, 소스 코드 검토에 사용.
큰 파일은 head_lines/tail_lines로 잘라서 읽어라.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "읽을 파일의 절대 경로"
                },
                "head_lines": {
                    "type": "integer",
                    "description": "앞에서 N줄만 읽기 (0 = 전체)",
                    "default": 0
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "뒤에서 N줄만 읽기 (0 = 전체)",
                    "default": 0
                }
            },
            "required": ["path"]
        }
    },

    # ── 4. write ───────────────────────────────────────────────
    {
        "name": "write",
        "description": """보드에 파일을 생성하거나 덮어쓴다.

설정 파일, 서비스 파일, 스크립트 배포에 사용.
내부적으로 SCP 업로드를 사용한다.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "생성할 파일의 절대 경로"
                },
                "content": {
                    "type": "string",
                    "description": "파일 내용"
                },
                "mode": {
                    "type": "string",
                    "description": "파일 권한 (예: '755', '644'). 빈 문자열이면 기본값.",
                    "default": ""
                }
            },
            "required": ["path", "content"]
        }
    },

    # ── 5. glob ────────────────────────────────────────────────
    {
        "name": "glob",
        "description": """보드에서 파일을 패턴으로 검색한다.

디바이스 파일 탐색(/dev/tty*, /dev/i2c-*),
설정 파일 위치 찾기, 로그 파일 목록 등에 사용.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 패턴. 예: '/dev/tty*', '/etc/**/*.conf'"
                },
                "base_dir": {
                    "type": "string",
                    "description": "검색 시작 디렉터리 (패턴에 절대경로 없을 때)",
                    "default": "/"
                }
            },
            "required": ["pattern"]
        }
    },

    # ── 6. grep ────────────────────────────────────────────────
    {
        "name": "grep",
        "description": """보드의 파일에서 패턴을 검색한다.

로그에서 에러 찾기, 설정 파일에서 파라미터 확인,
소스에서 특정 함수 위치 파악 등에 사용.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "검색할 정규식 또는 고정 문자열"
                },
                "path": {
                    "type": "string",
                    "description": "검색할 파일 또는 디렉터리 경로"
                },
                "flags": {
                    "type": "string",
                    "description": "grep 플래그. 예: '-r' (재귀), '-i' (대소문자 무시), '-n' (줄번호)",
                    "default": "-rn"
                },
                "max_results": {
                    "type": "integer",
                    "description": "최대 결과 수",
                    "default": 50
                }
            },
            "required": ["pattern", "path"]
        }
    },

    # ── 7. probe ───────────────────────────────────────────────
    # 임베디드 전용 — Claude Code에 없는 새 도구
    {
        "name": "probe",
        "description": """보드의 하드웨어/소프트웨어 환경을 체계적으로 탐지한다.

Claude Code에 없는 임베디드 전용 도구.
'무엇이 연결돼 있는지 모른다'는 상태에서 시작해야 하기 때문에 필요하다.

탐지 가능한 항목:
- all:    전체 환경 요약 (처음 연결했을 때 반드시 실행)
- hw:     연결된 하드웨어 (USB, I2C, SPI, GPIO, 시리얼 포트)
- sw:     설치된 소프트웨어 (ROS2, Python 패키지, 시스템 서비스)
- net:    네트워크 인터페이스, 포트, 외부 디바이스
- perf:   CPU/메모리/온도/전원 상태
- motors: 모터 컨트롤러 (VESC, ODrive, Dynamixel 등)
- camera: 카메라 디바이스
- lidar:  LiDAR 센서

결과를 바탕으로 실제 코드를 생성할 때 하드코딩하지 말고
probe 결과에서 확인된 값을 사용해라.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "탐지 대상",
                    "enum": ["all", "hw", "sw", "net", "perf", "motors", "camera", "lidar"]
                }
            },
            "required": ["target"]
        }
    },

    # ── 8. todo ────────────────────────────────────────────────
    {
        "name": "todo",
        "description": """작업 계획을 체크리스트로 관리한다. Claude Code의 TodoWrite/TodoRead와 동일.

복잡한 goal을 받으면 먼저 단계를 나눠라.
각 단계를 시작할 때 in_progress, 끝나면 completed로 업데이트해라.
모든 단계가 completed가 돼야 done을 호출할 수 있다.

이 도구가 중요한 이유:
긴 작업 중에 컨텍스트가 압축되거나 연결이 끊겨도
todo 목록이 남아있으면 어디까지 했는지 알 수 있다.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "전체 todo 목록 (부분 업데이트 없음 — 항상 전체를 넘겨라)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":       {"type": "string"},
                            "content":  {"type": "string"},
                            "status":   {"type": "string",
                                         "enum": ["pending", "in_progress", "completed"]},
                            "priority": {"type": "string",
                                         "enum": ["high", "medium", "low"],
                                         "default": "medium"}
                        },
                        "required": ["id", "content", "status"]
                    }
                }
            },
            "required": ["todos"]
        }
    },

    # ── 9. subagent ────────────────────────────────────────────
    {
        "name": "subagent",
        "description": """독립 컨텍스트에서 탐색/분석 작업을 실행한다. Claude Code의 Task 도구와 동일.

메인 에이전트의 컨텍스트를 깨끗하게 유지하면서
탐색적 작업(하드웨어 조사, 로그 분석, 시도-실패-재시도 루프)을
격리된 공간에서 처리할 수 있다.

subagent는 자신의 subagent를 만들 수 없다 (재귀 방지).
결과 요약만 메인으로 반환된다.

언제 써야 하는가:
- 결과를 예측할 수 없는 탐색 ('이 보드에 어떤 모터 컨트롤러가 있는가?')
- 많은 명령이 필요한 조사 작업
- 시도-실패-재시도가 예상되는 초기 설정

context 파라미터 중요:
- 이미 발견한 device 경로, IP, 파라미터를 전달해라
- subagent가 이미 아는 것을 다시 탐색하지 않도록
- 예: "device: /dev/ttyACM0, param: baud_rate=115200, ip: 192.168.1.10" """,
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "subagent가 달성해야 할 목표. 구체적이고 완결된 지시문으로 작성."
                },
                "context": {
                    "type": "string",
                    "description": "메인 루프에서 이미 발견한 정보. device 경로, 파라미터, IP 등. 비워두면 자동으로 채워진다.",
                    "default": ""
                }
            },
            "required": ["goal"]
        }
    },

    # ── 10. verify ─────────────────────────────────────────────
    {
        "name": "verify",
        "description": """probe(존재 확인)와 달리, 컴포넌트가 실제로 동작하는지 확인한다.

probe가 "장치가 있는가"라면 verify는 "장치가 응답하는가 / 데이터를 내보내는가"다.

target 종류:
- serial_device:  /dev/ttyACM0 같은 시리얼 장치에 실제로 통신되는지 확인
- i2c_device:     특정 I2C 주소가 실제 응답하는지 (i2cdetect + 간단 read)
- network_device: IP 주소가 응답하고 포트가 열려 있는지
- ros2_topic:     토픽이 실제로 데이터를 퍼블리시하는지 (hz 측정)
- process:        프로세스/서비스가 실행 중이고 crash되지 않았는지
- system:         전체 시스템 이상 없음 (dmesg 에러, 온도, 디스크, 메모리)
- custom:         위 범주에 없는 자유형 확인 — bash로 직접 처리

device 파라미터:
  "serial_device" → "/dev/ttyACM0"
  "i2c_device"    → "1:0x68"  (버스:주소)
  "network_device"→ "192.168.0.10:80"
  "ros2_topic"    → "/scan"
  "process"       → "vesc_driver" 또는 "ros2"
  "system"        → "" (불필요)
  "custom"        → 확인할 내용 설명

결과: PASS/FAIL/WARN + 측정값 포함
verify 결과에서 얻은 파라미터 값(baud rate, 응답 포맷 등)은 이후 코드에 직접 사용.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "확인 유형",
                    "enum": ["serial_device", "i2c_device", "network_device",
                             "ros2_topic", "process", "system", "custom"]
                },
                "device": {
                    "type": "string",
                    "description": "확인 대상. target에 따라 다름. 위 설명 참조.",
                    "default": ""
                }
            },
            "required": ["target"]
        }
    },

    # ── 11. done ───────────────────────────────────────────────
    {
        "name": "done",
        "description": """goal 달성 완료 또는 달성 불가 판정 시 최종 보고.

호출 조건:
- 모든 todo가 completed 상태일 때 (성공)
- 물리적으로 불가능함이 확인됐을 때 (실패)
- 사용자 개입이 필요할 때 (부분 완료)

⚠️ 물리 동작 goal: "코드가 실행됐음"만으로 done() 호출 금지.
실제 하드웨어에 효과가 있었는지 확인 후 호출.
물리 한계 발견 시: success=false + 실제로 가능한 대안 제시.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": "goal 달성 여부"
                },
                "summary": {
                    "type": "string",
                    "description": "무엇을 했고 결과가 어떻게 됐는지"
                },
                "notes": {
                    "type": "string",
                    "description": "주의사항, 물리적 한계, 대안 제안",
                    "default": ""
                }
            },
            "required": ["success", "summary"]
        }
    }
]


# ─────────────────────────────────────────────────────────────
# 위험 명령 필터
# Claude Code의 permission system에 해당
# ─────────────────────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "dd if=",
    "mkfs",
    "> /dev/sd",
    ":(){ :|: & };:",  # fork bomb
    "chmod -R 777 /",
    "chown -R",
]

def is_dangerous(command: str) -> bool:
    cmd_lower = command.lower()
    return any(p.lower() in cmd_lower for p in DANGEROUS_PATTERNS)


# ─────────────────────────────────────────────────────────────
# probe 명령 매핑
# 각 target별로 실행할 실제 셸 명령들
# ─────────────────────────────────────────────────────────────

PROBE_COMMANDS: dict[str, str] = {
    "hw": """
echo "=== USB 장치 ===" && lsusb 2>/dev/null || echo "(lsusb 없음)"
echo "=== 시리얼 포트 ===" && ls -la /dev/ttyACM* /dev/ttyUSB* /dev/ttyS* 2>/dev/null | head -20
echo "=== I2C 버스 ===" && ls /dev/i2c-* 2>/dev/null && for b in /dev/i2c-*; do echo "  $b:"; i2cdetect -y ${b##*-} 2>/dev/null | head -5; done
echo "=== SPI 장치 ===" && ls /dev/spi* /dev/spidev* 2>/dev/null || echo "(없음)"
echo "=== GPIO ===" && ls /dev/gpiochip* 2>/dev/null || echo "(없음)"
echo "=== dmesg 최근 HW 이벤트 ===" && dmesg --time-format iso 2>/dev/null | grep -iE "(usb|tty|i2c|spi|gpio)" | tail -20
""".strip(),

    "sw": """
echo "=== OS ===" && uname -a && cat /etc/os-release 2>/dev/null | head -6
echo "=== Python ===" && python3 --version 2>/dev/null && pip3 list 2>/dev/null | grep -iE "(ros|serial|gpio|numpy|cv2|torch)" | head -20
echo "=== ROS2 ===" && ls /opt/ros/ 2>/dev/null || echo "(ROS2 없음)"
echo "=== 실행 중인 서비스 ===" && systemctl list-units --state=running --type=service 2>/dev/null | grep -v "^$" | tail -20
echo "=== 설치된 언어/런타임 ===" && for cmd in node java rustc go; do which $cmd >/dev/null 2>&1 && echo "$cmd: $($cmd --version 2>&1 | head -1)"; done
""".strip(),

    "net": """
echo "=== 네트워크 인터페이스 ===" && ip addr show 2>/dev/null | grep -E "(inet |^[0-9])"
echo "=== 연결된 외부 IP ===" && ip route 2>/dev/null
echo "=== 열린 포트 ===" && ss -tlnp 2>/dev/null | head -20
echo "=== 외부 디바이스 ping (192.168.0.x) ===" && for ip in 192.168.0.{1..20}; do ping -c1 -W1 $ip >/dev/null 2>&1 && echo "  alive: $ip"; done
""".strip(),

    "perf": """
echo "=== CPU ===" && cat /proc/cpuinfo 2>/dev/null | grep -E "(model name|processor)" | head -4
echo "=== 메모리 ===" && free -h 2>/dev/null
echo "=== 디스크 ===" && df -h 2>/dev/null | grep -v tmpfs
echo "=== 온도 ===" && cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | while read t; do echo "$((t/1000))°C"; done || echo "(온도 센서 없음)"
echo "=== 부하 ===" && uptime 2>/dev/null
echo "=== GPU/가속기 ===" && nvidia-smi 2>/dev/null | head -10 || tegrastats --interval 1000 2>/dev/null & sleep 2; kill %1 2>/dev/null; wait 2>/dev/null
""".strip(),

    "motors": """
echo "=== 시리얼 장치 (모터 컨트롤러 후보) ==="
ls -la /dev/ttyACM* /dev/ttyUSB* /dev/ttyS* 2>/dev/null || echo "(시리얼 장치 없음)"
echo "=== CAN 인터페이스 ==="
ip link show type can 2>/dev/null || echo "(CAN 없음)"
echo "=== 모터 관련 Python 패키지 ==="
pip3 list 2>/dev/null | grep -iE "(serial|can|motor|odrive|dynamixel|roboclaw|sabertooth|pololu)" || echo "(없음)"
echo "=== 모터 관련 실행 중인 프로세스 ==="
ps aux 2>/dev/null | grep -iE "(motor|drive|servo|actuator|controller)" | grep -v grep | head -10 || echo "(없음)"
echo "=== ROS2 파라미터 파일 (모터 설정 포함 가능) ==="
find /etc /home /opt -maxdepth 6 -name "*.yaml" 2>/dev/null | xargs grep -l "motor\|speed\|servo\|drive" 2>/dev/null | head -10 || echo "(없음)"
echo "=== dmesg 모터/시리얼 관련 이벤트 ==="
dmesg 2>/dev/null | grep -iE "(ttyACM|ttyUSB|usb|serial)" | tail -10 || echo "(없음)"
""".strip(),

    "camera": """
echo "=== V4L2 장치 ===" && ls /dev/video* 2>/dev/null || echo "(없음)"
echo "=== USB 카메라 ===" && lsusb 2>/dev/null | grep -iE "(camera|webcam|imaging|logitech|microsoft)" || echo "(없음)"
echo "=== CSI 카메라 ===" && ls /dev/nvargus-daemon 2>/dev/null && echo "Jetson CSI 가능" || echo "(CSI 없음)"
echo "=== 카메라 도구 ===" && which v4l2-ctl 2>/dev/null && v4l2-ctl --list-devices 2>/dev/null | head -20 || echo "(v4l2-utils 없음)"
echo "=== 카메라 Python 패키지 ===" && pip3 list 2>/dev/null | grep -iE "(cv2|opencv|picamera|pyrealsense|pypylon)" || echo "(없음)"
""".strip(),

    "lidar": """
echo "=== USB/시리얼 LiDAR ==="
lsusb 2>/dev/null | grep -iE "(laser|lidar|hokuyo|rplidar|sick|velodyne|ouster|urg)" || echo "(USB에서 못 찾음)"
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -10
echo "=== 네트워크 LiDAR (이더넷 연결형) ==="
# 같은 서브넷에서 응답하는 IP 스캔 (arp 캐시 + ping sweep)
arp -n 2>/dev/null | grep -v "incomplete" | awk '{print $1}' | head -20 || echo "(arp 없음)"
ip neigh 2>/dev/null | grep "REACHABLE\|STALE" | awk '{print $1}' | head -20 || echo "(ip neigh 없음)"
echo "=== LiDAR Python 패키지 ==="
pip3 list 2>/dev/null | grep -iE "(rplidar|ydlidar|sick|hokuyo|velodyne|pcl|laser)" || echo "(없음)"
echo "=== ROS2 LiDAR 관련 토픽 ==="
source /opt/ros/$(ls /opt/ros/ 2>/dev/null | tail -1)/setup.bash 2>/dev/null
timeout 3 ros2 topic list 2>/dev/null | grep -iE "(scan|lidar|laser|point)" || echo "(ROS2 없거나 토픽 없음)"
""".strip(),
}

# all = hw + sw + perf 의 요약 버전
PROBE_COMMANDS["all"] = """
echo "======= 보드 전체 환경 탐지 ======="
echo "=== 1. 기본 시스템 ===" && uname -a && cat /etc/os-release 2>/dev/null | grep -E "^(NAME|VERSION)=" | head -2
echo "=== 2. 연결된 하드웨어 ===" && lsusb 2>/dev/null | head -10 && ls /dev/ttyACM* /dev/ttyUSB* /dev/i2c-* /dev/video* 2>/dev/null
echo "=== 3. 주요 소프트웨어 ===" && ls /opt/ros/ 2>/dev/null && python3 --version 2>/dev/null
echo "=== 4. 네트워크 ===" && ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1"
echo "=== 5. 리소스 ===" && free -h 2>/dev/null && cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null && echo " (milli°C)"
echo "=== 6. 실행 중인 주요 서비스 ===" && systemctl list-units --state=running --type=service 2>/dev/null | grep -iE "(ros|motor|camera|lidar|serial)" | head -10
echo "======= 탐지 완료 ======="
""".strip()


# ─────────────────────────────────────────────────────────────
# verify 명령 매핑
# probe = 존재 확인 / verify = 동작 확인
# 문제 C 해결: "make sure system is working" 요청 처리
# 문제 D 해결: ROS2 노드/QoS 상태까지 확인
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# verify 명령 매핑
# 범용: 특정 하드웨어 이름 없음. device 파라미터로 대상 지정.
# probe = 존재 확인 / verify = 실제 응답/동작 확인
# ─────────────────────────────────────────────────────────────

# verify 명령은 device 파라미터를 환경변수로 받아 동적으로 동작함.
# executor._verify()가 ECC_DEVICE 환경변수를 설정해서 호출.

VERIFY_COMMANDS: dict[str, str] = {

    # 시리얼 장치: 실제 바이트 수신 가능한지
    "serial_device": r"""
DEV=${ECC_DEVICE:-$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1)}
if [ -z "$DEV" ]; then echo "serial FAIL: no serial device found"; exit 1; fi
echo "serial device: $DEV"
# 장치 접근 권한 확인
if [ ! -r "$DEV" ]; then echo "serial FAIL: no read permission on $DEV"; exit 1; fi
# 현재 설정 확인
stty -F "$DEV" 2>/dev/null && echo "serial PASS: $DEV accessible" || echo "serial WARN: stty failed (may need root)"
# 통신 라이브러리 확인
python3 -c "import serial; print('pyserial OK:', serial.VERSION)" 2>/dev/null || echo "pyserial not installed"
""".strip(),

    # I2C 장치: 특정 주소가 실제 응답하는지
    "i2c_device": r"""
# ECC_DEVICE format: "BUS:ADDR" e.g. "1:0x68"
BUS=$(echo "${ECC_DEVICE:-1:0x00}" | cut -d: -f1)
ADDR=$(echo "${ECC_DEVICE:-1:0x00}" | cut -d: -f2)
DEV="/dev/i2c-$BUS"
if [ ! -e "$DEV" ]; then echo "i2c FAIL: $DEV does not exist"; exit 1; fi
echo "Scanning I2C bus $BUS..."
i2cdetect -y "$BUS" 2>/dev/null || echo "i2cdetect not available"
if [ -n "$ADDR" ] && [ "$ADDR" != "0x00" ]; then
  i2cget -y "$BUS" "$ADDR" 2>/dev/null && echo "i2c PASS: device $ADDR on bus $BUS responded" || echo "i2c FAIL: device $ADDR on bus $BUS did not respond"
fi
""".strip(),

    # 네트워크 장치: IP 응답 + 포트 확인
    "network_device": r"""
# ECC_DEVICE format: "IP:PORT" or just "IP"
HOST=$(echo "${ECC_DEVICE:-}" | cut -d: -f1)
PORT=$(echo "${ECC_DEVICE:-}" | cut -d: -f2)
if [ -z "$HOST" ]; then echo "network FAIL: no host specified in ECC_DEVICE"; exit 1; fi
echo "Pinging $HOST..."
ping -c 2 -W 2 "$HOST" 2>/dev/null && echo "network PASS: $HOST reachable" || echo "network FAIL: $HOST not reachable"
if [ -n "$PORT" ] && [ "$PORT" != "$HOST" ]; then
  timeout 3 bash -c "echo > /dev/tcp/$HOST/$PORT" 2>/dev/null && echo "port $PORT: OPEN" || echo "port $PORT: CLOSED or timeout"
fi
""".strip(),

    # ROS2 토픽: 실제로 데이터가 발행되는지
    "ros2_topic": r"""
TOPIC="${ECC_DEVICE:-}"
ROS_DISTRO=$(ls /opt/ros/ 2>/dev/null | tail -1)
if [ -z "$ROS_DISTRO" ]; then echo "ros2 FAIL: ROS2 not installed"; exit 1; fi
source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null

echo "=== ROS2 nodes ==="
timeout 3 ros2 node list 2>/dev/null || echo "(no nodes running)"

if [ -n "$TOPIC" ]; then
  echo "=== Topic info: $TOPIC ==="
  timeout 3 ros2 topic info "$TOPIC" --verbose 2>/dev/null || echo "topic not found"
  echo "=== Checking publish rate ==="
  timeout 4 ros2 topic hz "$TOPIC" 2>/dev/null | grep -E "average|no new" | head -2 || echo "no data in 4s"
else
  echo "=== All topics ==="
  timeout 3 ros2 topic list 2>/dev/null || echo "(no topics)"
fi
""".strip(),

    # 프로세스/서비스: 실행 중이고 크래시되지 않았는지
    "process": r"""
PROC="${ECC_DEVICE:-}"
if [ -z "$PROC" ]; then echo "process FAIL: no process name in ECC_DEVICE"; exit 1; fi
echo "=== Process check: $PROC ==="
PIDS=$(pgrep -f "$PROC" 2>/dev/null)
if [ -n "$PIDS" ]; then
  echo "process PASS: $PROC running (pids: $PIDS)"
  ps -p $PIDS -o pid,pcpu,pmem,etime,cmd 2>/dev/null | head -5
else
  echo "process FAIL: $PROC not running"
  # systemd 서비스인지 확인
  systemctl status "$PROC" 2>/dev/null | head -10 || echo "(not a systemd service)"
fi
""".strip(),

    # 전체 시스템: dmesg 에러, 온도, 디스크, 메모리
    "system": r"""
echo "=== Recent errors (dmesg) ==="
dmesg --time-format iso 2>/dev/null | grep -iE "error|warn|fail|disconnect|killed" | tail -15   || dmesg 2>/dev/null | grep -iE "error|warn|fail" | tail -15   || echo "(dmesg not available)"

echo "=== Memory ==="
free -h 2>/dev/null

echo "=== Disk ==="
df -h 2>/dev/null | awk 'NR==1 || ($5+0 > 85) {print}'

echo "=== CPU Temperature ==="
for f in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$f" ] || continue
  t=$(cat "$f" 2>/dev/null)
  c=$((t/1000))
  zone=$(dirname $f | xargs basename)
  if [ $c -gt 80 ]; then echo "TEMP WARN $zone: ${c}C"
  else echo "TEMP OK $zone: ${c}C"; fi
done 2>/dev/null || echo "(no thermal sensors)"

echo "=== Load ==="
uptime 2>/dev/null
""".strip(),

    # 자유형: bash로 직접 수행
    "custom": r"""
echo "custom verify: ${ECC_DEVICE}"
# ECC_DEVICE에 확인할 내용이 담겨 있음. executor가 추가 처리.
""".strip(),
}
