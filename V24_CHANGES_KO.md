# KnitCode v2.4 — 타입 힌트로 매개변수 메서드 호출 추론

## 목적

`def go(ctx: Context): ctx.invoke()`와 같은 호출은 기존 v2.3.1에서 `ctx`의 타입을 몰라 미해결이었습니다. v2.4는 **소스에 명시된 타입 힌트를 추론 근거로만 사용**하며 프로젝트 내부 `Context.invoke()` 정의를 찾으면 정적 *may-call* 간선을 추가합니다.

## 실제 구현

1. `ast_parser.py`: 함수·메서드의 `parameter_annotations`를 소스 텍스트로 저장합니다.
2. `flow_analysis.py`: 함수 진입 시 단순 이름·점표기 이름·문자열 전방 참조를 `annotated_parameter` 후보로 기억합니다. 호출 지점까지의 대입 흐름에서 복사·재할당·삭제·임포트 재바인딩·분기 병합을 고려합니다.
3. `graph_builder.py`: 힌트가 실제 프로젝트 내부 **CLASS** 하나로 연결되는 경우에만 그 클래스의 메서드(필요한 경우 내부 부모 클래스 메서드)를 탐색합니다. 외부 클래스·없는 클래스·모호한 타입은 이름만 보고 추측하지 않습니다.
4. `call_status.py`: 타입 힌트 기반 연결은 **MAY_CALL**로 분류하며 `graph_result.json` 간선에는 `evidence.rule=instance_parameter_type_hint`, `evidence.certainty=declared_parameter_type_may_call` 등의 근거를 보관합니다.
5. `verify_v24.py`: 구조 16개 + 호출 상태 10개 + 타입 힌트 4개, 선택적 소스 위치 검사를 수행합니다. 이는 **의미적 정답 검증이 아닙니다.**

### 지원 범위

- `ctx: Context`, `ctx: "Context"`, `ctx: m.Context`, `from ... import Context as C`와 같은 단순 타입 힌트
- 일반·비동기 함수, 키워드 전용·위치 전용 매개변수, 지역 변수 별칭, 내부 상속된 메서드
- 매개변수 재할당 후에는 새로운 도달 정의(reaching definition)를 우선하고, 가능한 경로가 섞이면 MAY_CALL 후보로 표시

### 명시적 한계

- Python 타입 힌트는 실행 시 타입을 보장하지 않습니다. **“RESOLVED_INTERNAL”은 정적 출처가 있는 연결 후보**이지 런타임 호출의 정답이 아닙니다.
- `Optional[Context]`, `Context | None`, 제네릭/프로토콜, 타입 변수, 함수 인자 실제 전달 관계, C3 전체 다형성 후보는 지원하지 않습니다.
- 데이터 흐름 엔진도 완전한 CFG·동적 import·reflection·Monkey patch·런타임 재바인딩 분석은 아닙니다.
- 외부 패키지 코드를 실행하지 않습니다.

## 실행 — Windows PowerShell

압축을 푼 뒤 `main.py`가 있는 폴더에서:

```powershell
python main.py "C:\Users\c\Downloads\click\src" --output-dir output_click
python verify_v24.py --graph output_click\graph_result.json --symbols output_click\symbol_result.json --repo "C:\Users\c\Downloads\click\src"
python -m unittest discover -s tests -q
start output_click\report.html
```

빠른 예제:

```powershell
python main.py examples\v24_types --output-dir output_demo
python verify_v24.py --graph output_demo\graph_result.json --symbols output_demo\symbol_result.json --repo examples\v24_types
start output_demo\report.html
```

`report.html`의 `Edges`에서 `instance_parameter_type_hint` 검색 또는 전체 상태에서 호출 이름(`ctx.invoke`)을 검색하면 근거를 확인할 수 있습니다. v2.3.1 출력은 별도 폴더에 보관하고 동일한 소스와 커밋만 비교하세요.

## 비교 실험 (작성 환경의 설치된 Click 코드 기준)

- 설치된 Click **16개 Python 파일**(2026-10-05 작성 환경, 사용자가 이전에 고정한 Click 커밋과 다른 소스 스냅샷)
- v2.3.1: 550 심볼, 1,755 호출, **348 내부 연결**, 1,407 미연결, 741 간선
- v2.4:  550 심볼, 1,755 호출, **364 내부 연결**, 1,391 미연결, 757 간선
- 새 내부 메서드 호출 연결 **16개**, 기존에 연결된 호출의 손실 **0개**
- 새로운 연결의 예: `ctx.make_formatter → Context.make_formatter`, `ctx.invoke → Context.invoke`, `ctx.fail → Context.fail`.
- 구조·상태 검사와 AST 위치 검증 모두 통과. 이 숫자는 **정확도/재현율이 아니라 연결 범위**입니다.

v2.4는 v2.3.1과 달리 그래프 간선이 의도적으로 바뀝니다. `verify_v231.py --old ...`의 간선 동일성 검사를 통과해야 하는 버전이 아닙니다. 대신 `verify_v24.py --old old_graph_result.json`은 동일 입력에서 변경을 요약합니다.
