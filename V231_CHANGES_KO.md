# KnitCode v2.3.1 — 내부·비내부·미확정 호출 경계 분리

**이 버전은 새로운 호출 간선을 만들거나 기존 간선을 제거하지 않습니다.** 원래 v2.3의 10개 미해결 휴리스틱 범주를 유지하고, `symbol_result.json`의 AST 사실과 `graph_result.json`의 간선에 근거해 *모든* 호출 지점의 상태를 추가합니다.

## 1. 상태와 확실성

- `RESOLVED_INTERNAL`: 분석기가 해당 호출 지점에 대해 소스에 존재하는 내부 노드로 `CALLS`/`INSTANTIATES` 간선을 생성한 상태. 간선은 **may-call** 사실일 수 있으며 완전한 런타임 호출 대상 집합을 의미하지 않습니다.
- `KNOWN_NON_INTERNAL`: 가려지지 않은 내장 이름, 또는 조건부·중복 import/재할당 충돌이 발견되지 않은 이름에서 표준 라이브러리·외부 패키지·내장 함수 **출처**를 추적한 상태. 실제 속성 존재·런타임 monkey patch까지 증명한다는 의미는 아닙니다.
- `UNKNOWN`: 객체 타입 불명, 함수 별칭, 프로젝트 내부 후보, 기본 자료형 메서드에 대한 *휴리스틱 추정*, 복잡한 동적 이름 참조, 또는 이름 충돌 가능성. **내부 호출을 놓쳤다는 판정이 아닙니다.**

`certainty`는 `SOURCE_BACKED`, `MAY_CALL`, `PROVEN_STATIC_ORIGIN`, `UNVERIFIED`를 사용합니다. 숫자로 된 신뢰 확률은 만들지 않습니다. `legacy_category`는 기존 미해결 휴리스틱 분류를 그대로 노출합니다.

## 2. 보수적인 근거 조건

- 재수출 import 체인의 최종 출처를 따라가되, 프로젝트 내부 모듈이라는 사실만으로 내부 함수로 연결하거나 비내부 호출로 확정하지 않습니다.
- 변수 할당/재할당, 조건부 import, `def` 재정의, 지역 매개변수와 같은 이름 충돌을 확인합니다.
- `global`, `nonlocal`, `:=`, `del`, `with ... as`, `except ... as`, `from ... import *`의 정적 재바인딩 위험도 확인합니다.
- 단순 `data=[]; data.append()`처럼 **타입 휴리스틱만 제공하는 경우 `KNOWN_NON_INTERNAL`로 확정하지 않습니다.** 기존 `BUILTIN_METHOD` 후보는 남깁니다.
- v2.3의 옛 `symbol_result.json`에는 신규 재바인딩 사실(`binding_hazards`)이 없으므로, `reclassify_existing.py`로 옛 결과만 처리하면 비내부 출처가 **UNKNOWN**으로 보수적으로 표시됩니다. 신규 출처 확인을 원하면 원본 소스를 `main.py`로 다시 분석해야 합니다.

**한계:** 분석기 자체가 완전한 Python CFG/전역 이름 환경을 증명하는 것은 아닙니다. monkey patching, 리플렉션, 런타임 import/exec, 동적으로 추가되는 속성, 외부 환경에 의한 이름 변경 등은 여전히 추적하지 못합니다. 결과는 정적 소스 기반 관찰이며 정답 데이터·정밀도(Precision/Recall) 평가가 아닙니다.

## 3. 새 출력 파일

- `call_statuses.json`: 모든 호출에 `call_id`, `status`, `origin_kind`, `certainty`, `target_ids`, `legacy_category`, `evidence`를 기록합니다.
- `call_statuses.csv`: 같은 호출 데이터의 Excel 호환 UTF-8 BOM CSV.
- `graph_result.json`: 새 `call_statuses[]`, `status_summary`를 **추가**. 기존 그래프 노드/간선/호출 카운트 및 미해결 카테고리 그대로 유지.
- `report.html`: **All call statuses** 탭과 세 상태 필터 및 상태별 집계 추가.

## 4. Windows PowerShell 사용법

압축을 풀고 **`main.py`가 들어 있는 폴더**에서 실행합니다. `--repo`에는 분석 시의 입력 루트를 그대로 전달합니다.

```powershell
python -m unittest discover -s tests -q

# Click (17개 Python 파일, v2.3 기준 2054 호출)
python main.py "C:\Users\c\Downloads\click\src" --output-dir output_click
python verify_v231.py --graph output_click\graph_result.json --symbols output_click\symbol_result.json --repo "C:\Users\c\Downloads\click\src"

# Requests
python main.py "C:\Users\c\Downloads\requests-main\requests-main\src" --output-dir output_requests
python verify_v231.py --graph output_requests\graph_result.json --symbols output_requests\symbol_result.json --repo "C:\Users\c\Downloads\requests-main\requests-main\src"

# Sublist3r
python main.py "C:\Users\c\Downloads\knitcode_sublist3r_v1\knitcode_sublist3r_v1\Sublist3r" --output-dir output_sublist3r
python verify_v231.py --graph output_sublist3r\graph_result.json --symbols output_sublist3r\symbol_result.json --repo "C:\Users\c\Downloads\knitcode_sublist3r_v1\knitcode_sublist3r_v1\Sublist3r"
```

호출 간선 동일성을 검증하려면 **동일 Git 커밋/분석 루트**로 생성한 기존 v2.3 `graph_result.json`을 별도 보관한 후 `--old` 옵션으로 넘겨주세요.

```powershell
python verify_v231.py --graph output_click\graph_result.json --symbols output_click\symbol_result.json --old "C:\path\to\v23_graph_result.json"
```

## 5. 현 배포본에서 확인한 내용

- 76개 단위·통합 테스트 통과 (v2.3의 57개 + v2.3.1의 19개).
- 신규 호출 상태 무결성 검증 **10개 PASS** + 기존 v2.3 그래프 무결성 검증 **16개 PASS**.
- 합성 fixture `mini` 53개 호출 지점, 34개 심볼의 AST 소스 위치 대조 PASS.
- **원본 v2.3을 별도로 실행하여 비교:** 동일 mini fixture의 그래프 노드, 간선, 카운터, 미해결 분류/이유 **변경 없음**.
- 이 zip에 Click·Requests·Sublist3r 실제 Git 저장소는 포함되지 않습니다. 사용자 PC에서 동일 소스 루트로 재분석하여 새 세 상태의 실제 프로젝트별 분포를 확인해야 합니다.
- `mini/bad_syntax.py`의 파싱 오류 1개는 예외 처리 테스트를 위한 의도적 오류입니다.
