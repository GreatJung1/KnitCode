# KnitCode v2.4 확장 실험 — 기본 자료형 추론 및 의존성 소스 분석

> 이 확장판은 기존 v2.4 Call Graph를 보존하면서 내장 자료형 판정과 라이브러리 **소스 기반 후보 추론**을 추가한다. 모든 Python 런타임 호출을 해결하거나 외부 C 확장 구현을 정적으로 복원하는 것은 아니다.

## 1. 바뀐 기능

### A. 기본 자료형 메서드의 출처 확인

- `s = ','; s.join(xs)`: AST의 문자열 대입과 호출 시점의 흐름을 확인해 `str.join`으로 판정
- `s = ','.join(xs); s.strip()`: 제한적인 `str` 반환 타입 전파
- `a = []; a.append(x)`, `d = {}; d.get('x')`, `x = dict(); x.keys()` 등: 확실한 컬렉션 타입에 한하여 내장 메서드로 판정
- `if/else`에서 다른 타입으로 재할당되거나, 타입을 모르는 객체의 `obj.join()`은 `UNKNOWN` 유지
- 저장소 내부 `MyClass.join()`이 연결되면 **내부 호출이 우선**이며 내장 호출로 덮어쓰지 않음
- 현재 분석 범위: 일부 `str`, `bytes`, `list`, `dict`, `set`, `tuple` 메서드. `int`, 복잡한 alias, 사용자 정의 컨테이너 및 모든 Python 내장 메서드의 완전 지원은 아님

### B. 표준 라이브러리/외부 라이브러리 코드까지 분석 (옵션)

기존 버전은 `import os` 등의 출처만 판정했다. 이제 `--analyze-deps`를 주면 **가져온 라이브러리의 `.py` 소스**를 AST로 파싱한다.

- 명시적 import와 모듈 별칭을 따라 라이브러리 함수/클래스/메서드의 실제 소스 정의 위치 확인
- 일부 상대 import, 단순 재수출(`from .api import get`), 정적 `__all__`이 있는 `import *` 추적
- 객체 생성자에 대한 단순 흐름 추적 후 `obj.method()`의 라이브러리 메서드 후보 확인
- 라이브러리 소스 내의 직접 함수 호출, `self.method()`, 단순 모듈 호출을 별도의 그래프에 저장
- `.py`가 없고 `.pyi`만 있으면 API 정보만 참고하며 실행 구현을 찾았다고 주장하지 않음
- `os.path.join()`은 대상 플랫폼에 따라 구현이 달라질 수 있으므로 `posixpath.join`/`ntpath.join` 후보와 **플랫폼 의존** 근거를 기록

**중요:** 동적 디스패치, monkeypatch, 리플렉션, C extension 내부 코드, 동적 재수출, 환경마다 달라지는 API는 완전하게 분석하지 못한다. 호출 대상이 의심스러우면 후보 또는 `ORIGIN_ONLY_NO_DEFINITION`으로 남긴다. 라이브러리 소스는 **절대 import/실행하지 않고** `ast.parse`로만 읽는다.

## 2. 실행 방법

기존 정적 분석(이전과 동일):

```bash
python main.py /path/to/repo --output-dir output
```

표준 라이브러리 + **현재 Python 환경에 설치된 외부 패키지** 추가 분석:

```bash
python main.py /path/to/repo --analyze-deps --output-dir output
```

프로젝트용 가상환경의 의존성 소스를 명시적으로 분석(권장):

```powershell
py main.py C:\path\to\repo --analyze-deps `
  --dep-root C:\path\to\repo\.venv\Lib\site-packages `
  --max-dep-files 500 --output-dir output
```

- `--dep-root`: 다른 소스 루트 경로를 반복 지정 가능 (예: `site-packages/`, 특정 패키지 폴더)
- `--max-dep-files`: 무한 범위 탐색을 방지하는 파싱 파일 상한(기본 300)
- 기본적으로 실행 중인 Python의 표준 라이브러리와 설치 패키지를 탐색하므로, **대상 프로젝트에서 사용한 Python 버전 및 의존성 버전**과 일치하도록 실행하는 편이 정확하다.
- 외부 패키지 소스를 자동으로 다운로드하거나 pip 설치하지 않는다. 설치되지 않은 의존성을 분석하려면 소스 디렉터리를 `--dep-root`로 전달한다.

## 3. 출력 파일

`graph_result.json`, `call_statuses.json/.csv`, `report.html` 등 기존 결과는 이전과 같이 **분석 대상 저장소 내부** 그래프와 상태를 나타낸다.

`--analyze-deps` 사용 시 추가로 다음 두 파일 생성:

- `dependency_graph.json`: `.py/.pyi`에서 찾은 의존성 정의 노드, **프로젝트 → 의존성 호출 후보** `project_edges`, 의존성 소스 내 직접 호출 후보 `dependency_edges`, 소스 접근성·근거·요약
- `dependency_calls.csv`: 호출별 실제 라이브러리 소스 정의 탐색 결과 (`SOURCE_FOUND`, `API_STUB_ONLY`, `ORIGIN_ONLY_NO_DEFINITION`)

프로젝트 그래프의 기존 `RESOLVED_INTERNAL/KNOWN_NON_INTERNAL/UNKNOWN` 상태는 그대로 유지한다. 즉 `KNOWN_NON_INTERNAL`은 소스 정의가 없다는 뜻이 아니며 `dependency_calls.csv`에서 따로 소스 위치가 확인될 수 있다. **상태를 혼합해서 기존 내부 호출 수가 늘어난 것처럼 표시하면 안 된다.**

## 4. 알려진 한계 및 후속 개발

1. **모든 라이브러리 호출 100% 연결은 불가능**: C/native extension, 런타임 생성 속성, 동적 import, 데코레이터 래핑 등은 AST만으로 확인 불가.
2. 단순 API 재수출 및 첫 번째 상속 기반 타입 후보만 보수적으로 처리. 정확한 C3 MRO, 예외 흐름, 완전한 다형성은 미지원.
3. 외부 클래스 및 메서드의 **정적 후보**는 실제 실행 대상과 다를 수 있다. 정의 위치를 찾는 것과 런타임 정확도는 구분.
4. 재귀 라이브러리 분석은 `--max-dep-files`로 제한한다. 잘린 경우 `dependency_graph.json`의 `truncated`와 `unindexed` 확인.
5. 시각화/웹 UI 및 Git 커밋 diff 분석은 이 확장판 범위 밖. 필요하면 다음 통합 단계에 적용.

## 5. 회의용 요약

**원래 v2.4는 저장소 내부 Call Graph 정확도에 집중**했다. 이 확장판은 변수 대입을 통한 내장 자료형의 메서드 출처 판정을 확장하고, 표준/외부 라이브러리의 Python 소스를 AST로 파싱해 **내부 그래프와 분리된 의존성 호출 후보 그래프**를 추가했다. 소스가 실제로 존재할 때만 위치를 연결하고, 알 수 없을 때는 미확정으로 남겨 정확도 저하를 피한다.
