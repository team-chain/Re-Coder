# Core 실행 로그

VS Code 확장이 실행한 Core의 출력과 종료 정보는 `~/.recoder/core.log`에 저장된다. 디버그 콘솔 출력도 유지한다. 확장이 기록하므로 Python import 실패, traceback, 강제 종료의 종료 코드·신호도 남길 수 있다.

## 확인 방법

macOS 터미널에서:

```bash
tail -n 80 ~/.recoder/core.log
```

계속 확인하려면:

```bash
tail -F ~/.recoder/core.log
```

`Ctrl+C`는 로그 보기를 끝낸다. Core나 AWS 서비스를 중지하는 명령은 아니다.

각 줄에는 UTC 시각, 확장 호스트 PID, Core PID, `stdout`·`stderr`·`lifecycle` 구분이 붙는다. 프로세스 생성 전에는 Core PID가 `?`로 표시된다.

```text
2026-09-23T00:00:00.000Z [host=100 pid=200] [lifecycle] spawned
2026-09-23T00:00:01.000Z [host=100 pid=200] [stdout] [ReCoder Core] Starting ...
2026-09-23T00:01:00.000Z [host=100 pid=200] [lifecycle] restart requested
2026-09-23T00:01:01.000Z [host=100 pid=200] [lifecycle] exited code=0 signal=null
```

`restart requested`나 `stop requested`와 종료 기록을 함께 확인한다. `SIGTERM`은 정상 재시작에서도 발생할 수 있으며, `SIGKILL`은 강제 종료 신호다. 신호만으로 누가 종료했는지 또는 근본 원인을 단정할 수 없다. `spawn error`, `startup failed`는 프로세스 생성·준비 단계의 실패를 뜻한다.

## 보관 및 제한

- 파일당 최대 2MiB, 백업 `core.log.1`부터 `.3`까지 보관한다. 총 최대 약 8MiB이며 오래된 로그부터 제거한다.
- 재시작 시 기존 파일에 이어 쓴다.
- POSIX에서는 로그 파일 권한을 `0600`으로 제한한다.
- 확장이 알고 있는 토큰·키 값과 일반적인 인증 헤더·자격증명 필드를 가린다. 임의 애플리케이션 출력의 모든 개인정보를 자동 식별하는 기능은 아니므로 외부 공유 전 내용을 확인한다.
- 16,384자를 넘는 출력 줄은 메모리·파일 사용을 제한하고 비밀 값의 일부가 남지 않도록 줄 전체를 생략한다.
- 로그 저장 실패는 Core 실행을 중단하지 않으며 디버그 콘솔에 한 번 경고한다.

기록 대상은 **이 버전의 확장이 새로 실행한 Core**다. 기존 Core에 연결만 한 상태에서는 과거 출력을 복구할 수 없다. 확장을 다시 로드한 뒤 `ReCoder: Restart Core`를 한 번 실행하면 새 Core의 출력이 기록된다. 터미널에서 직접 실행한 Core의 출력은 이 파일로 수집되지 않는다. 확장 호스트 자체가 갑자기 종료되면 마지막 종료 이벤트가 남지 않을 수도 있다.

## 실기기 검증

AWS를 켜지 않고 진행할 수 있다.

1. 확장 컴파일 후 Extension Development Host를 다시 연다.
2. `ReCoder: Restart Core`를 실행한다.
3. `core.log`에 `spawned`, Core의 시작 출력, `ready port=...`가 남는지 확인한다.
4. 다시 `ReCoder: Restart Core`를 실행하고 이전 PID의 종료 정보와 새 PID의 시작 정보가 함께 남는지 확인한다.
5. Core 정상 응답과 기존 배포 기록 유지를 확인한다.

과거에 발생한 원인 불명의 재시작을 이 변경으로 소급 분석할 수는 없다. 재현 시 해당 시각 전후 로그를 통해 원인을 조사한다.
