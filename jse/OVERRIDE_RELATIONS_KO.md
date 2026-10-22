# 구조적 재정의 관계 — 2026-10-22

## 구현 전 확인

- `ast_parser.FactsVisitor`는 클래스 bases/decorators/class_keywords, 메서드 scope/decorators/control_depth 및 소스 위치를 수집한다. `scan_repository`는 소스를 실행하지 않는다.
- `symbol_builder.build_symbol_result`는 CLASS/METHOD 심볼과 canonical ID를 만든다. overload 선언은 구현과 분리하고, 중복 메서드는 마지막 정의와 alternate_definition_lines로 표현한다. 프로퍼티 접근자마다 독립 ID를 만들지는 않는다.
- `graph_builder.Resolver`는 클래스·심볼·대입·import 인덱스를 갖는다. `_base_order`는 선언 순서와 import 출처 및 불확실성을 보존한다. `_mro`는 C3 병합을 수행하며 complete/unknown/invalid를 구분하고 DFS로 대체하지 않는다.
- `_local_method`, `_lookup_mro`, `_find_method`에는 기존 상속 메서드 탐색과 가림 처리가 있다. `_resolve_super`와 object-flow는 이미 C3 기반 호출을 해석한다. 이 기능들은 호출 대상 선택용이며 별도 구조적 재정의 간선을 생성하지 않았다.
- `Resolver.build`의 기존 JSON은 nodes/edges, source/target/type/evidence와 호출 통계를 사용한다. 기존 관계는 CONTAINS/INHERITS/CALLS/INSTANTIATES다.
- LJY `analyze_project.py`의 호출 분석부는 `declared and recv` 또는 `self.method()` 조건에서 `all_desc`의 구현을 찾아 **caller → descendant method** 형태의 `calls_override`를 생성한다. 이는 호출 지점에 의존하는 동적 디스패치 후보다. 이번 작업에서는 해당 코드를 복사하거나 LJY 분석기를 실행하지 않았다.

## 구현과 의미

`Parent.run → Child.run`, type=`OVERRIDDEN_BY`를 기존 edges 배열에 추가한다. 호출이 전혀 없어도 생성된다. 자식이 직접 정의하지 않고 상속만 받으면 생성하지 않는다.

관계 범위는 **같은 멤버를 실제 정의한 모든 조상**이다. 예를 들어 A.run, B(A).run, C(B).run이면 A→B, A→C, B→C를 출력한다. 다중 상속에서도 자식의 C3 순서대로 실제 정의 조상을 연결한다. 이 순서와 `ancestor_mro_index`를 기록하되, 모든 조상이 호출된다는 의미는 아니다. 형제 클래스끼리는 연결하지 않는다. 중간 클래스가 같은 이름의 속성으로 가려도, 더 위 조상의 명시적 선언과 자식의 명시적 선언 사이의 구조적 관계라는 정의는 동일하다.

기존 `_mro`와 일반 메서드의 `_local_method`를 재사용한다. 새 관계는 모든 기존 호출 해석이 끝난 뒤 추가되며 호출 추론의 입력으로 사용하지 않는다. 시그니처 일치나 타입 호환성 검사는 하지 않는다. 이름과 상속에 따른 선언 관계이므로 인자 수, async 여부, 메서드 종류가 달라도 관계가 존재할 수 있다.

지원 종류는 instance_method, staticmethod, classmethod, 단일 property getter다. 종류 변경도 근거에 명시한다. property를 CALLS 대상으로 승격하지 않는다. 사용자 정의 또는 가려진 동명 데코레이터는 확정하지 않는다.

## 변경 파일과 함수

| 파일 | 변경 |
|---|---|
| `override_relations.py` | `build_override_edges`: 관계 생성, `_class_declarations`: 보관된 AST 소스로 무조건적 클래스 선언 확인, `_members`: 직접 정의와 가림 검증, `_decorator_is_builtin`: 데코레이터 이름 가림 검사, `overridden_methods`: 자식→부모 역조회 |
| `graph_builder.py` | `Resolver.build`: 호출 처리 뒤 관계를 추가하는 연결 지점만 변경 |
| `graph_report.py` | 관계 필터에 OVERRIDDEN_BY 추가, `select`/`drawFocus`에서 구조적 관계 선택 시 해당 관계만 표시 |
| `main.py` | 출력 간선 유형 안내 갱신 |
| `tests/test_override_relations.py` | 신규 테스트 14개 및 하위 사례 |
| `validation/override_regression.py` | 수정 전 입력/그래프 캡처 및 수정 후 정확 비교 |
| `validation/override_fixture/app.py` | 실제 CLI/JSON/HTML 출력 검증 예제 |
| `README.md`, 이 문서 | 사용법·의미·검증·한계 설명 |

`KnitCode-ljy`와 `KnitCode_jse/KnitCode_3` 복사본은 수정하지 않았다.

## JSON 및 역조회

실제 전체 출력은 `validation/override_export/graph_result.json`에 있다. 주요 필드 예시:

```json
{
  "source": "METHOD:app.Parent.run",
  "target": "METHOD:app.Child.run",
  "type": "OVERRIDDEN_BY",
  "evidence": {
    "file": "override_fixture/app.py",
    "line": 7,
    "rule": "source_defined_ancestor_member_c3",
    "relation_scope": "all_defining_ancestors",
    "mro": ["app.Child", "app.Parent", "builtins.object"],
    "ancestor_mro_index": 1,
    "parent": {"id": "METHOD:app.Parent.run", "file": "override_fixture/app.py", "line": 2, "member_kind": "instance_method"},
    "child": {"id": "METHOD:app.Child.run", "file": "override_fixture/app.py", "line": 7, "member_kind": "instance_method"},
    "signature_checked": false
  }
}
```

실제 parent/child에는 canonical_name, column, end_line, end_column, async도 포함된다. `call_id`는 없다.

```python
from override_relations import overridden_methods
incoming = overridden_methods(graph, 'METHOD:app.Child.run')
parent_ids = [edge['source'] for edge in incoming]
```

역방향 간선을 중복 저장하지 않으며 반환값은 근거를 포함한 원래 간선 목록이다.

## 검증

- 수정 전 기존 JSE 전체: **265개 통과**.
- 수정 후 기존 265개 + 새 14개: **279개 통과**.
- 수정 전 테스트 실행에서 저장한 **255개 입력/그래프**를 그대로 재실행: 새 OVERRIDDEN_BY와 증가한 edge_count만 제외한 **기존 그래프 전체 완전 일치**. 기존 **CALLS 217개**의 대상과 evidence까지 일치한다. 노드·미해결·모호성·조건부 결과·object-flow 통계도 비교에 포함된다.
- 새 테스트는 단일/간접/다중/C3 다이아몬드, 상속만 하는 경우, 무관한 동명 메서드, 부모의 정의 부재, super와 CALLS 분리, 시그니처 차이, descriptor 종류 변경, 외부·잘못된 MRO, 삭제·대입·import 가림, 조건부·중복 선언, private 이름, async, 파일 간 import 별칭과 위치, JSON 직렬화와 역조회를 검증한다.
- super 예제는 후처리 `reclassify_graph`/`annotate_call_statuses`까지 포함하여 관계 추가를 끈 결과와 비교한다.
- Windows 샌드박스에서는 임시 디렉터리 접근 거부가 발생하여 전체 테스트는 승인받은 샌드박스 외부 환경에서 실행했다.
- 로그: `validation/override_before_tests.log`, `override_capture.log`, `override_after_tests.log`, `override_replay.log`, `override_verify.log`.

재현 명령(현재 JSE 디렉터리):

```powershell
python -m unittest discover -s tests -v
python validation/override_regression.py replay
python main.py validation/override_fixture --output-dir validation/override_export
python verify_v231.py --graph validation/override_export/graph_result.json --symbols validation/override_export/symbol_result.json --repo validation/override_fixture
```

`override_baseline.pkl.gz`는 수정 전에 이 작업에서 생성한 신뢰된 테스트 스냅샷이다. pickle은 외부 파일로 대체하지 말 것. `capture`를 재실행하면 기준을 덮어쓰므로 전후 비교에는 보관된 파일과 `replay`를 사용한다.

## 호환성과 한계

기존 필드와 노드 ID, 기존 간선의 순서·내용을 유지하고 edges 배열에만 새 유형을 추가한다. edge_count는 전체 간선 수이므로 증가한다. 호출 통계와 call_statuses는 변하지 않는다. 기존 JSE 검증기 및 후처리와 호환된다. 외부 소비자가 간선 유형을 폐쇄형 enum으로 제한했다면 OVERRIDDEN_BY 허용이 필요하다. 모든 edges를 호출로 취급하는 소비자는 원래부터 CONTAINS/INHERITS도 섞이므로 CALLS로 필터링해야 한다.

불확실한 MRO(외부 부모 포함)는 일부 알려진 조상이 있더라도 전체 관계 생성을 생략한다. object/Generic/ABC의 MRO 전용 노드를 실제 메서드 정의로 만들지 않는다. 클래스 커스터마이징, 조건부/중복 클래스와 메서드, 다중 property 접근자, 알 수 없는 데코레이터(abstractmethod 포함), private name mangling, 소스 없는 입력 등은 보수적으로 제외한다. getter/setter/deleter별 심볼 모델 확장은 이번 범위가 아니다. 클래스 속성 재할당이 정의보다 앞선 안전한 경우도 생략할 수 있다.

이것은 소스 선언 관계이며 런타임 클래스 생성, monkey patching, exec, 데코레이터/메타클래스의 임의 부작용 및 실제 객체 디스패치를 증명하지 않는다. LJY calls_override 기능과 시그니처 호환성 검사도 구현하지 않았다.
