# JSE clean 정확성·성능 개선 결과

대상: `C:/Users/c/Downloads/KnitCode_B/KnitCode_jse_clean` (2026-10-22).
원래 `KnitCode_jse`, LJY, PJH 및 분석 대상 프로젝트 소스는 수정하지 않았다. Git 초기화/푸시/PR/병합 없음.

## 복구 기준과 변경 범위

작업 전 77개 파일을 `validation/accuracy_performance/before.zip`으로 백업하고 `before_sha256.json`에 해시를 저장했다. 기존 런타임 모듈 변경은 `object_flow.py`, `graph_builder.py` 두 개다. `class_decorators.py`와 `tests/test_accuracy_improvements.py`를 새로 추가했다. README와 이 보고서는 관련 문서다. AST 수집기, C3 구현, 구조적 OVERRIDDEN_BY 구현은 변경하지 않았다.

전체 소스 diff: `validation/accuracy_performance/implementation.patch`.

## 1단계: with as 객체 바인딩

**원인:** `ObjectFlow.block`이 컨텍스트 표현식만 방문하고 모든 as 변수에 UNKNOWN을 할당했다. Requests `src/requests/api.py:71`의 `session.request()`가 UNKNOWN인 것을 저장된 수정 전 결과에서 재현했다. `sessions.py:505`의 Session.__enter__는 실제로 self를 반환한다.

**수정:** `ObjectFlow.context_protocol`을 추가해 기존 `_find_method`와 `invoke`를 재사용한다. 클래스에서 __enter__를 조회하고 반환 Value와 allocation site를 유지해 self 반환/다른 객체 반환/알 수 없는 반환을 구분한다. 다중 컨텍스트는 왼쪽부터 진입하고, exit는 역순으로 처리한다. __exit__의 필드 변경은 정상 흐름 및 반환 시점의 heap에도 반영한다. 성공한 with 다음에는 이전 as 변수의 값이 섞이지 않는다.

프로토콜 자체에 가짜 call_id나 명시적 CALLS를 만들지 않는다. __enter__/__exit__ 구현 내부에 실제 AST Call이 있는 경우는 기존 호출 지점으로 분석한다. 알 수 없는 매니저는 실제로 전달된 객체와 도달 가능한 필드만 무효화한다. 매니저와 관계없는 객체를 지우는 중간 회귀는 최소 재현으로 수정했다.

Requests의 해당 호출은 `METHOD:requests.sessions.Session.request`, `object_flow_may_call`, certainty=MAY_CALL로 바뀌었다. 이름만 보고 Manager의 메서드로 연결하지 않는다. async with는 awaitable 실행을 해석하지 못하므로 계속 UNKNOWN이다.

## 2단계: self 내부 호출

**원인:** Rich의 지적된 5개 호출 모두 재현됐다. 클래스의 @rich_repr 또는 @dataclass 때문에 `_mro`가 unknown_class_customization을 반환하고 직접 정의된 메서드까지 누락됐다. attrs `_version_info.py` 원본은 Downloads/프로젝트에서 찾지 못했으므로 실제 attrs 사례 개선을 주장하지 않는다.

**수정:** `class_decorators.preserves_local_method`에서 범위를 엄격히 제한한 근거 검사를 수행한다. 외부 부모나 metaclass가 없는 클래스의 일반 직접 정의 메서드만 대상으로 한다. 출처가 확인된 dataclasses.dataclass는 일반 메서드를 보존하는 모델을 사용한다. 소스가 있는 decorator는 입력 클래스를 그대로 반환하고 repr 관련 속성만 변경하는 제한적 AST 패턴을 검사한다. Rich rich_repr → auto → do_replace 흐름이 이 범위에 해당한다. 임의 함수 호출, 클래스 교체, 계층/일반 메서드 변경, helper 재바인딩, 알 수 없는 decorator는 허용하지 않는다. 클래스 전체의 C3나 override 관계를 새로 인증하지 않는다.

`Resolver._find_method`는 위 근거가 있을 때만 직접 정의를 반환한다. `_mro`는 그대로 유지한다. `Resolver.resolve`는 실제 수신 매개변수, staticmethod의 일반 매개변수, 재바인딩, 중첩 함수의 가림을 구분한다. 바깥 메서드의 수신 객체를 캡처한 중첩 함수/람다는 보존한다. 중간 Click 비교에서 이 캡처 4개가 빠지는 회귀를 찾아 수정했고 최종 비교에서는 손실이 없다.

| 실제 소스 호출 | 수정 후 대상 |
|---|---|
| rich/style.py:712 self._make_ansi_codes | Style._make_ansi_codes (정의 344행) |
| rich/pretty.py:457 self.iter_tokens | Node.iter_tokens (정의 424행) |
| rich/layout.py:217 self.get | Layout.get |
| rich/layout.py:305 self.split | Layout.split |
| rich/layout.py:313 self.split | Layout.split |

## 3단계: 상속 메서드 호출

**원인:** 기존 탐색 진입점이 self/cls라는 철자에 의존했다. `def go(this): this.run()`처럼 실제 첫 매개변수 이름이 다르면 단일/간접/C3 다중 상속에서도 UNKNOWN이 되는 사례를 재현했다.

**수정:** `Resolver.resolve`에서 심볼의 실제 첫 수신 매개변수를 확인한 뒤 기존 `_find_method`/C3를 사용한다. 이름 가림·재할당을 검사하고 super의 기존 별도 경로도 유지한다. 완전한 단일·간접·다이아몬드 MRO에서 정확한 정의를 확인하는 신규 테스트를 추가했다.

Sublist3r에서는 enumratorBase 내부의 self.print_ 및 self.get_response는 이미 해결돼 있었다. 자식들의 누락은 `enumratorBaseThreaded(multiprocessing.Process, enumratorBase)`에서 앞선 외부 부모의 MRO를 확인할 수 없기 때문이다. 여기서 enumratorBase의 메서드나 super의 생성자를 확정하면 위험하므로 UNKNOWN을 유지했다. **Sublist3r 실제 결과의 CALLS 증가 없음**을 실패 은폐 없이 기록한다.

## 4단계: 객체 전달·성능

**실제 누락:** `def consume(*, worker): worker.run()`에 Worker()를 전달해도 키워드 전용 매개변수에는 context 태그가 없어서 호출 결과를 기존 해석에 전달하지 못했다. `ObjectFlow.invoke`에서 위치 인자와 동일한 문맥 태그를 붙였다. 서로 다른 호출의 객체/heap은 섞지 않는 테스트를 추가했다. Bound Method/Callable/콜백은 기존 구현을 재사용했다.

**프로파일 근거:** 수정 전 Rich에서 ObjectFlow refs 누적 약 55.0초, imports_at 약 38.0초, merge_maps 약 40.6초, 반복 own_nodes 약 23.1초가 관측됐다. 누적 시간은 서로 겹치므로 합산하지 않는다.

**최적화:**

- ObjectFlow.refs의 정적인 이름 해석만 `(name, file, module, scope, line)` 키로 분석 인스턴스 내부에서 캐시한다. 객체·heap·반환 요약을 호출 간 공유하는 캐시는 추가하지 않았다.
- 함수별 자기 AST 노드 목록과 generator 여부를 한 번 계산한다.
- 불변 Value의 동일 값 join 및 이미 있는 tag 추가는 기존 값을 반환한다.
- 실제 전달 인자가 있으면 사용하지 않을 annotation hint를 미리 계산하지 않는다.
- 진입점별 6,000 step, 재귀 깊이 16 제한은 그대로다.

## 같은 소스 전후 비교

예제·테스트를 포함해 각 프로젝트 폴더 전체를 같은 옵션으로 스캔했다. 전후 `analysis_sources`가 완전히 동일하며 소스 digest를 `comparison.json`에 기록했다. 프로젝트당 180초 timeout을 적용했고 최종 5개 모두 정상 완료했다.

| 프로젝트 | 이전 시간(s) | 최종 시간(s) | UNKNOWN 이전→최종 | 제한 도달 이전→최종 | CALLS 추가/삭제 |
|---|---:|---:|---:|---:|---:|
| schedule | 1.10 | 0.92 | 409 → 409 | 0 → 0 | +0 / -0 |
| Sublist3r | 0.64 | 0.50 | 210 → 210 | 0 → 0 | +0 / -0 |
| requests | 8.43 | 4.59 | 666 → 665 | 0 → 0 | +1 / -0 |
| click | 14.61 | 9.69 | 1714 → 1714 | 128 → 128 | +0 / -0 |
| rich | 162.55 | 62.32 | 1904 → 1762 | 1767 → 1813 | +143 / -0 |

Rich 시간에는 전후 모두 cProfile 비용이 포함된다. 예전 비프로파일 벤치마크의 65.4초와 직접 비교할 수 없다. 나머지는 비프로파일 wall time이다. 각 표 수치는 해당 실행의 단회 측정이며 환경 잡음이 있다. Rich는 같은 측정 조건에서 약 61.7% 감소했다.

기존 CALLS **8,497개는 모두 보존**, OVERRIDDEN_BY **159개는 순서와 evidence까지 동일**하다. 새 구조적 재정의 간선은 추가하지 않았다. Requests 1개 및 Rich 143개 CALLS가 추가됐다. Rich의 UNKNOWN은 142개 감소했으며 나머지 1개는 기존 해결 호출 지점의 후보 확장이다.

해당 추가 후보는 Rich `tests/test_console.py:637~640`에서 SystemPager._pager에 mock_pager를 실제 대입한 뒤 console.pager 컨텍스트를 종료하는 경로다. `SystemPager.show`의 self._pager 호출에서 mock_pager가 분석된다. 근거 없는 동명 메서드 탐색이 아니라 기존 필드/콜백 흐름을 __exit__ 경로에서 재사용한 결과이며 certainty=interprocedural_may_call이다. 구조적 간선 또는 확정 실행으로 표현하지 않는다.

Rich expression_steps는 1,541,940 → 2,072,051, 제한 도달은 1,767 → 1,813으로 늘었다. 새로 추적하는 메서드·컨텍스트 흐름이 늘어난 결과이며 제한 도달 건수는 FN 건수가 아니다. 성능 개선을 제한 도달 감소나 UNKNOWN 무조건 감소로 주장하지 않는다.

## 정확성 검증과 한계

Requests의 명시적 request 호출과 위 Rich 5개는 원본 정의를 직접 확인한 FN 개선 사례다. 신규 음성 테스트에서 무관한 타입/가려진 self/불확실한 enter/클래스 교체 decorator/불완전 MRO를 연결하지 않음을 확인했다. **추가된 144개 간선 전체에 런타임 정답 오라클을 붙여 FP=0을 증명한 것은 아니다.** 이 수치를 144개의 수동 검증 TP로 해석하지 않는다. 신규 간선별 호출·대상 정의 위치는 `added_calls_source_review.json`, 전체 변화는 `final_delta_*.json`에 있다.

CALLS는 기존 엔진의 may-call 의미와 evidence.certainty를 유지한다. 별도 동적 디스패치 후보 기능을 추가하지 않았고, 동일 이름이나 미사용 자식 클래스만으로 후보를 생성하지 않았다. JSON 최상위 구조·기존 공개 API·간선 유형은 유지된다.

남은 한계: async with/awaitable 실행, context manager의 예외 억제/예외 경로 완전 해석, 외부 부모 클래스 MRO, 임의 클래스 decorator·metaclass, 속성/호출의 동적 부작용, kwargs/가변 인자의 모든 의미론은 해결하지 않았다. decorator 근거 검사는 제한적이며 supports되지 않는 표현식은 거절한다. 새로운 정적 캐시도 분석 중 입력 심볼이 불변이라는 기존 전제를 따른다.

## 테스트와 산출물

최종 **305개 실행 / 305개 통과 / 실패 0 / 오류 0 / 건너뛰기 0**: 기존 279 + 신규 26. 4단계별 최소 재현의 실패를 확인한 뒤 관련 테스트만 실행했다. 초기 전체 299개 통과 후 실제 프로젝트 비교가 찾은 회귀 때문에 수정·추가 검증했고 최종 305개 전체를 다시 확인했다. 대형 프로젝트 재실행도 이 회귀 수정 확인을 위해 수행했다. 중간 결과는 intermediate/after, 최종 결과는 final 접두사로 구분한다.

최초 테스트들은 승인된 외부 실행을 사용했다. 전체 테스트 외부 실행이 거부된 뒤에는 다시 요청하지 않고, 작업 폴더 내부 test_tmp를 사용하는 샌드박스 실행으로 전체 검증을 완료했다.

신규 테스트:

- `WithBindingTests.test_enter_returns_self`
- `WithBindingTests.test_enter_returns_other_object`
- `WithBindingTests.test_unknown_enter_not_manager`
- `WithBindingTests.test_enter_preserves_identity_and_rebinding`
- `WithBindingTests.test_multiple_and_nested`
- `WithBindingTests.test_async_unknown`
- `WithBindingTests.test_no_synthetic_protocol_calls`
- `WithBindingTests.test_after_with_does_not_keep_old_binding`
- `WithBindingTests.test_exit_effects_on_returned_alias`
- `WithBindingTests.test_unknown_manager_does_not_invalidate_unrelated_object`
- `SelfBindingTests.test_dataclass_local_method`
- `SelfBindingTests.test_source_proven_repr_decorator`
- `SelfBindingTests.test_unknown_class_decorator_not_erased`
- `SelfBindingTests.test_non_receiver_self_parameter`
- `SelfBindingTests.test_rebound_self_and_field_shadow`
- `SelfBindingTests.test_fake_dataclass`
- `SelfBindingTests.test_closure_and_lambda_capture_receiver`
- `SelfBindingTests.test_closure_parameter_shadows_receiver`
- `SelfBindingTests.test_decorator_changes_method_or_hierarchy`
- `SelfBindingTests.test_decorator_helper_rebound`
- `InheritedBindingTests.test_receiver_name_is_not_magic`
- `InheritedBindingTests.test_named_receiver_c3_diamond`
- `InheritedBindingTests.test_opaque_preceding_base_stays_unknown`
- `KeywordFlowTests.test_keyword_only_receiver`
- `KeywordFlowTests.test_keyword_contexts_do_not_share_heap`
- `KeywordFlowTests.test_named_receiver_rebinding`

주요 결과는 `validation/accuracy_performance/`에 있다:

- `before.zip`, `before_sha256.json`: 수정 전 복구 기준
- `implementation.patch`: 실제 코드 변경 diff
- `comparison.json`, `final_delta_*.json`: 시간·통계·간선 전후 비교
- `added_calls_source_review.json`: 신규 호출과 대상 정의의 위치
- `before_*`, `final_*`: 같은 소스의 그래프/심볼 스냅샷과 로그, Rich 프로파일
- `final_tests.log`, `final_tests.json`, `focused.log`: 테스트 결과
- `benchmark.py`: 재실행 가능한 제한시간 적용 벤치마크. `python -B validation/accuracy_performance/benchmark.py <새 접두사>`로 실행한다. 기존 before 결과를 덮어쓰지 말 것.

실제 프로젝트 코드는 import하거나 실행하지 않고 AST로 분석했다. 네트워크 요청 및 의존성 설치는 하지 않았다.
