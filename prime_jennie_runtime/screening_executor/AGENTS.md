# `screening_executor/` — 격리 샌드박스 코드 실행

Track D 소유. Scout가 생성한 Python 코드를 안전하게 실행.

## 격리 수준

- 컨테이너: non-privileged, non-root, `network: none`
- 리소스: 4GB 메모리, 2 CPU, 300s 타임아웃
- 파일시스템: read-only + `/tmp` tmpfs
- seccomp profile 적용

## Import 화이트리스트

```
pandas, numpy, scipy.stats, talib,
sklearn.cluster, sklearn.linear_model, sklearn.preprocessing, sklearn.metrics,
math, statistics, datetime
```

**명시 금지**: os, sys, subprocess, socket, importlib, ctypes, eval, exec, compile

## minyoung-mah 연동

`ScreeningToolAdapter` — `ToolAdapter` 프로토콜 구현. Scout의 `screening_code`를 받아 격리 컨테이너에서 실행하고 `list[ScreeningCandidate]` 반환.

## 필수 테스트

악의 코드 테스트 12건 이상 (M01~M12). 모두 거부되어야 함.
