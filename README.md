# KnitCode — Static Call Graph Prototype (v2.4)

Python 3.10+ / Windows PowerShell / **표준 라이브러리만 사용**. 이 파일은 기존 개인 프로토타입(v1)을 확장한 버전입니다. 팀원 프로토타입 코드는 통합하지 않았습니다.

## v2.4 — 매개변수 타입 힌트 기반 메서드 추론 (NEW)

- `def go(ctx: Context): ctx.invoke()` 등 **프로젝트 내부 클래스로 확인되는 단순 타입 힌트**를 통해 기존에 미해결이던 메서드를 연결합니다.
- 문자열 전방 참조(`"Context"`), 명시적 import 별칭, 상속 메서드, 키워드 전용/비동기 함수의 타입 힌트를 지원합니다.
- AST 데이터 흐름에서 파라미터가 **재할당·삭제·다른 객체로 변경**되면 기존 타입 힌트를 그대로 확신하지 않습니다. 분기에서 가능한 후보가 여러 개라면 `MAY_CALL`로만 기록합니다.
- 타입 힌트는 Python에서 실행 시 강제되지 않으므로, 타입 힌트 기반 호출은 그래프에 `instance_parameter_type_hint`와 `declared_parameter_type_may_call`, 호출 상태에 `MAY_CALL`로 표시합니다.
- `Optional[Context]`, `Context | None`, `list[Context]`, 복잡한 제네릭, 실제 다형성 디스패치 등은 자동 추측하지 않습니다.
- `verify_v24.py`와 `tests/test_v24.py`를 추가했습니다. 테스트 예제는 `examples/v24_types`에서 직접 실행할 수 있습니다.
- **전체 실행법·동일 소스 비교·실측 성과·제약사항:** [`V24_CHANGES_KO.md`](V24_CHANGES_KO.md).
- 참고: 아래 v2.3.1 기록의 “간선을 바꾸지 않았다”는 문장은 **그 당시 버전의 변경점**을 뜻합니다. v2.4는 새 간선을 추가합니다.

## v2.3.1 — 전체 호출 상태 분리 (NEW)

- 전체 AST 호출을 `RESOLVED_INTERNAL` / `KNOWN_NON_INTERNAL` / `UNKNOWN`으로 나눕니다.
- 호출 대상 출처·재할당 충돌·확실성·판정 규칙은 `call_statuses.json`, `call_statuses.csv`에 기록합니다.
- 오프라인 `report.html`에서 **All call statuses** 탭으로 전체 호출과 상태별 필터를 열 수 있습니다.
- 새 `verify_v231.py`로 기존 v2.3 16개 무결성 검사와 신규 호출 상태 10개 검사를 실행합니다.
- 기존 `graph_result.json` 간선, v2.3 호출 카운트와 미해결 재분류는 바꾸지 않았습니다.
- **전체 실행 지침과 한계는 [`V231_CHANGES_KO.md`](V231_CHANGES_KO.md)를 먼저 읽으세요.**

## v2.3 — 중복 심볼·재수출 import 검증

- `@overload` 타입 선언은 별도로 보관하고 실행 구현만 그래프 노드로 생성합니다.
- 상대 import 및 프로젝트 모듈을 통한 표준/외부/내부 함수 재수출을 추적합니다.
- 그래프 중복 노드·간선을 정리하고, 소스 위치를 교차 확인하는 범용 `verify_v23.py`를 추가했습니다.
- 이전 v2.2에 대한 설명과 Sublist3r 전용 `verify_v22.py`는 과거 버전 기록 및 비교를 위해 남겨 두었습니다.

## v2.2 — 함수 밖 내부 호출 + 클래스 컬렉션 추적

이 버전은 **v2.1을 기반으로 정적 호출 관계를 실제로 추가**하는 업데이트입니다.

1. **MODULE 노드**: 함수 밖에서 수행하는 프로젝트 내부 함수 호출(예: `if __name__ == "__main__": interactive()`)을 `MODULE:sublist3r → FUNCTION:sublist3r.interactive`와 같이 연결합니다. 모듈 안에서 정의되기 **이전**의 함수로 호출 간선을 추측하지 않습니다. `evidence.module_level=true`와 `evidence.certainty=module_initialization_may_execute`로 나타냅니다.
2. **클래스 컬렉션 추적**: 유한한 리터럴 클래스 리스트/튜플/집합, 딕셔너리의 클래스 값, 리스트의 `append`, `if/else` 분기 병합, `for`와 **리스트 컴프리헨션**의 반복 변수에 전달되는 클래스 참조를 근사 추적합니다. `supported_engines` 딕셔너리 → `chosenEnums` 리스트 → `enum()`으로 전달된 클래스 후보를 해석할 수 있습니다.
3. **may-call**: 여러 클래스를 선택할 수 있으면 하나를 임의로 확정하지 않고 후보별 `INSTANTIATES`(+ 정의된 `__init__`에 대한 `CALLS`) 간선을 만들고 `ambiguous_calls`와 `evidence.certainty=multiple_possible_targets`에 명시합니다. 외부 또는 알 수 없는 값이 섞이면 `conditional_may_call`로 표시합니다.
4. **기존 재분류 유지**: 미해결 호출의 `category`, `reason`, `scope_context`, `confidence`, CSV/HTML 보고서를 유지합니다. 새로 해결된 호출은 미해결 목록에서 빠집니다.

**주의:** v2.1 결과를 단순히 `reclassify_existing.py`로 갱신해도 새로운 간선은 만들어지지 않습니다. **`python main.py <Sublist3r 폴더>`로 전체 재분석**해야 합니다. 기존 v2.1 `output` 폴더는 비교용으로 보존하세요.

## v2.1 — 유지된 미해결 호출 진단

- `reason`: 호출 대상 해석을 실패한 코드상의 이유.
- `category`: 내장 함수/메서드, 표준/외부 라이브러리, 내부 후보, 간접 호출, 필드 체인 등의 10가지 휴리스틱 범주.
- `scope_context`: 함수 내부와 모듈 최상위를 별도 축으로 구분.
- `classification_evidence`와 `candidate_targets`: 분류 근거 및 미검증 후보. 함수 간 관계와는 별개입니다.
- JSON과 UTF-8 BOM CSV 및 오프라인 HTML로 열람할 수 있습니다.

## Windows 실행 — 기존 Sublist3r 저장소 재사용

압축을 풀고 `main.py`가 있는 폴더에서 아래 명령어를 실행합니다.

```powershell
# v1에서 이미 다운로드한 원본 Sublist3r 경로를 그대로 사용해도 됩니다.
python main.py "C:\Users\c\Downloads\knitcode_sublist3r_v1\knitcode_sublist3r_v1\Sublist3r"
python -m unittest discover -s tests -v
start output\report.html
```

새 폴더에서 Sublist3r를 다시 클론한 경우에는 `python main.py Sublist3r`로 실행합니다. **실제 Sublist3r 프로그램을 실행하거나 네트워크 스캔할 필요는 없습니다.** Git commit은 `output`의 JSON에 자동 기록합니다.

## 무엇이 달라졌나?

**v1:** 조건문 내부의 변수 대입을 `control_depth > 0`이라 판단하여 안전을 위해 해석하지 않았습니다. 따라서 같은 `if` 블록 안에서 `pscan = portscan(...)` 후 `pscan.run()`도 놓쳤습니다.

**v2 (v2.2에서도 유지):** `flow_analysis.py`를 추가했습니다. AST 문장 순서에 따라 추상 변수 상태를 전달하고, `if/else` 각 경로를 독립 분석한 뒤 가능한 타입을 합칩니다.

```python
if ports:
    pscan = portscan(subdomains, ports)
    pscan.run()   # portscan.run로 연결 (블록 안에서 유효)
```

```python
if flag:
    obj = A()
else:
    obj = B()
obj.run()  # may-call: A.run, B.run 2개 후보
```

```python
if flag:
    obj = A()
obj.run()  # A.run의 조건부 may-call, may_be_unbound=true
```

조건부 호출은 **무조건 발생한다고 확정하지 않습니다**. `output/graph_result.json`의 `conditional_calls`, Edge evidence `certainty` 및 `may_be_unbound` 필드로 별도 표시합니다. 호출 가능 대상이 복수이면 `ambiguous_calls`로 별도 저장합니다. 특정 호출에 근거가 없으면 `unresolved_calls`에 남깁니다.

## 출력에 추가된 통계

- `module_level_resolved_sites`: MODULE를 호출자로 연결한 call-site 수.
- `collection_indirect_resolved_sites`: 컬렉션 원소를 추적해 해결한 간접 call-site 수.
- `node_count`와 `edge_count`는 MODULE 노드 및 새 호출 간선 때문에 **v2.1과 달라질 수 있습니다**. 내부 함수의 정확도를 개수만으로 판단할 수는 없습니다.

## 산출물

- `output/ast_result.json`: 원본 AST facts 및 호출 위치의 흐름 스냅샷
- `output/symbol_result.json`: 심볼·import·assignment 및 flow snapshot 전달
- `output/graph_result.json`: `CONTAINS`, `INHERITS`, `CALLS`, `INSTANTIATES`, 호출 근거, 다중 후보, 조건부 호출, 미해결
- `output/report.html`: 인터넷 없이 열리는 HTML 보고서 (카테고리 버튼과 필터 포함)
- `output/unresolved_classification.json`: 재분류 결과 전체 + 요약
- `output/unresolved_classification.csv`: Excel에서 열어 정렬·검토 가능한 UTF-8 BOM CSV

## v2.1 ↔ v2.2 결과 비교

두 분석이 같은 Git commit인지 확인한 다음, v2 디렉터리에서 다음 명령을 실행하면 새로 해결한 호출, 잃어버린 연결 및 변경된 호출 대상을 출력합니다.

```powershell
python compare_results.py "C:\Users\c\Downloads\knitcode_sublist3r_v2_1 (1)\knitcode_sublist3r_v2_1\output\graph_result.json" output\graph_result.json
```

**개수가 늘어났다는 사실만으로 성능 개선이 입증되지는 않습니다.** 새 관계를 원본 코드와 검증해야 합니다.

## v2.2까지의 자동 테스트

`python -m unittest discover -s tests -v` — **46개 테스트 (v2.1 33개 + v2.2 13개)**: 기존 v1 기능 13개에서 기존 if 분기 기대 결과를 v2 의미에 맞게 변경하고 9개 회귀 테스트를 추가했습니다. 함수별 스코프, 변수 재할당, 조건문 안/밖, 두 분기 병합, 불확실한 외부 값, 중첩 함수, 함수 import와 상속 등을 검증합니다.

## 범위와 한계

- **구조화된 `if/else`와 단순 반복문 추상 상태를 다루는 경량 데이터 흐름 분석**이지, Python 전체를 대상으로 한 완전한 CFG·고정점 분석이나 일반적인 타입 추론기가 아닙니다.
- 반복 실행 간 상태 고정점, 예외를 포함한 복잡한 실행 경로, `break`/`continue`, 동적 호출(`getattr`, `eval`, monkey patch), 복잡한 comprehension·generator 표현식, 다중상속 C3 MRO 및 외부 라이브러리 객체의 정확한 메서드 타입은 보장하지 않습니다. `try/except`, `while`은 근사 병합만 지원합니다.
- `resolved_call_sites`/`unresolved_call_sites`는 자체 분류 결과이며, **Ground Truth 기반 Precision/Recall/F1 값이 아닙니다**. 다른 팀원과 비교하려면 같은 commit, 같은 내부 호출 정답 집합과 동일한 평가 규칙이 필요합니다.
- 사용자 PC에서 확인한 기존 v2.1 결과는 Sublist3r 호출 514개 중 117개 resolved/397개 unresolved입니다. **v2.2의 실제 Sublist3r 결과는 해당 저장소를 로컬에서 재분석한 뒤 검증**해야 합니다. 이 패키지는 그 원본 Sublist3r 파일을 포함하지 않습니다.
- `flow_analysis.py`는 리스트 원소의 **가능한 클래스 집합**을 추적합니다. `append`로 다른 리스트의 별칭을 통해 수정하는 경우, `extend`, 동적 dict 갱신, 복잡한 loop fixed-point는 지원하지 않습니다. 딕셔너리 키를 정적으로 모르면 가능한 값을 넓게 포함하므로 **false positive 가능성을 평가**해야 합니다.
- 전체 모듈 초기화의 조건/예외와 모듈 간 import 수행 순서를 완전히 실행 분석하지 않습니다. 클래스 본문 안에서 실행되는 호출은 별도 한계입니다.
- 자식 클래스 오버라이딩으로 발생하는 다형성 동적 디스패치(C3 MRO 등)는 **이번 단계에서는 변경하지 않았습니다**.


## v2.2 검증 도구 (추가)

`verify_v22.py`는 동일한 Sublist3r 커밋의 v2.1/v2.2 결과를 비교해 회귀 오류를 찾습니다. 원본 코드 실행 없이 JSON/AST를 검사합니다.

```powershell
python verify_v22.py --old "C:\Users\c\Downloads\knitcode_sublist3r_v2_1 (1)\knitcode_sublist3r_v2_1\output\graph_result.json" --new output\graph_result.json --repo "C:\Users\c\Downloads\knitcode_sublist3r_v1\knitcode_sublist3r_v1\Sublist3r"
```

`[PASS]`는 검사 통과, `[FAIL]`는 예상과 다른 결과이며 종료 코드 1을 반환합니다. `--repo`로 소스 AST 대조를 추가할 수 있습니다. `--old` 생략 시 그래프 무결성 및 7개 예제 검증만 수행합니다.

더 나아가 `gold_annotations.template.json`의 7개 사례를 원본 코드로 **직접 검토한 뒤** `reviewed: true`로 수정하면:

```powershell
python verify_v22.py --new output\graph_result.json --gold gold_annotations.template.json
```

검토 완료된 호출에 한해 TP/FP/FN과 Precision·Recall·F1을 계산합니다. **초안은 전부 `reviewed=false`이며, 이 값은 Sublist3r 전체 정확도가 아닙니다.** v2.1 대비 회귀검증과 정확도 평가를 혼동하지 마세요.

---

## v2.3: Requests cross-project hardening

v2.3 extends v2.2 and retains its static-only, no-target-execution behavior.
The following were changed:

1. **`@overload` typing stubs** are kept in `symbol_result.json` as
   `overload_declarations` metadata, and only an actual implementation (if
   present) creates a callable FUNCTION/METHOD node. Unimplemented overload-only
   stubs are not assumed callable. Non-overload repeated definitions have a
   deterministic last-definition fallback; dynamic conditional redefinition
   remains a limitation.
2. **Relative imports and chained re-exports** are traced through concrete
   module import facts (`from .compat import urlparse` ->
   `urllib.parse.urlparse`). Imports resolved to the stdlib/external dependencies
   do **not** fabricate internal call-graph edges. Conditional imports that
   point to mixed kinds are reported as `DYNAMIC_UNKNOWN`, with candidate
   origins in `classification_evidence`.
3. **Stable identities**: duplicate node IDs, `CONTAINS` relations and
   repeated call-site edges are deduplicated. Added static graph integrity
   checks and 11 cross-project fixture tests.

### Recommended Windows commands

```powershell
# Before upgrading: back up your v2.2 output elsewhere.
python -m unittest discover -s tests -v

# The repo folder must contain the installed package layout `requests` under `src`.
python main.py "C:\Users\c\Downloads\requests-main\requests-main\src" --output-dir output_requests
python verify_v23.py --graph output_requests\graph_result.json --symbols output_requests\symbol_result.json --repo "C:\Users\c\Downloads\requests-main\requests-main\src"

# Analyze the original pinned Sublist3r for a second cross-project regression.
python main.py "C:\Users\c\Downloads\knitcode_sublist3r_v1\knitcode_sublist3r_v1\Sublist3r" --output-dir output_sublist3r
python verify_v23.py --graph output_sublist3r\graph_result.json --symbols output_sublist3r\symbol_result.json --repo "C:\Users\c\Downloads\knitcode_sublist3r_v1\knitcode_sublist3r_v1\Sublist3r"
```

Optional comparison after saving the v2.2 graph as a separate file:

```powershell
python verify_v23.py --graph output_requests\graph_result.json --symbols output_requests\symbol_result.json --repo "C:\Users\c\Downloads\requests-main\requests-main\src" --old old_requests_graph_result.json
```

**Important:** This is not a true accuracy benchmark. Call resolution count
alone is not Precision, Recall, or F1. For teammate comparisons, use an identical
source commit, package root and call extraction conventions. ZIP downloads do
not carry a `.git` directory and the reported commit will be null. The example
Requests data uploaded for v2.2 consists of graph/classification JSON only;
full v2.3 rescan requires the original Python source checkout on your PC.
