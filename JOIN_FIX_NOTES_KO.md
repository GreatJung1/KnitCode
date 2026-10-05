# KnitCode v2.4 `join` 정적 판정 보완

이 패치는 **함수 호출 그래프 자체의 내부 해석 범위를 넓히지 않고**, 이미 미해결이었던 일부 호출에 대해 **내장 메서드/표준 라이브러리 출처를 판별**한다. v2.4의 데이터 흐름 및 심볼·Import 정보를 활용하며 분석 대상 코드를 실행하거나 import하지 않는다.

## 파일별 변경

- `join_origin.py` (**신규**): 호출 AST 원본에서 `str`/`bytes` 리터럴 수신 객체의 `.join()`을 검증한다.
- `unresolved_classifier.py`: 직접 리터럴 `.join`은 `BUILTIN_METHOD` 범주로 진단한다. 이 진단은 기존 휴리스틱 재분류이므로 상태 판정과 별도다.
- `call_status.py`: 안전한 리터럴 `.join()`을 `BUILTIN`으로 분류한다. 또한 `flow` 스냅샷의 모든 생성자 후보가 **출처가 검증된** `threading.Thread` 또는 `multiprocessing.Process`와 이의 상속 클래스일 때 `STDLIB`로 추론한다. 재할당·조건부 import·불명확한 선행 부모·지역 메서드 재정의를 피한다.
- `verify_v231.py`: 새 출처 근거 규칙들을 무결성 검사에서 인정한다.
- `tests/test_join_origin.py` (**신규**): 안전한 리터럴·문자열 연결 메서드·생성자·다중 후보, 외부 라이브러리 및 로컬 클래스의 이름 충돌, 재할당, monkeypatch, 조건부 import 등의 11개 검증 사례.

## Sublist3r 검증 결과

동일 Python 4개 파일, **514개 호출** 기준:

| 지표 | 수정 전 | 수정 후 |
| --- | ---: | ---: |
| 내부 호출 연결 | 124 | 124 |
| 비내부 판정 | 181 | **190** |
| 불명확 (`UNKNOWN`) | 209 | **200** |
| Call Graph 간선 | 242 | **242** |

**직접 `.join()` 13개** 중 문자열 리터럴 7개, `os.path.join` 4개, `threading.Thread.join` 1개, 내부 클래스가 `multiprocessing.Process`로부터 상속한 `join` 1개를 비내부 호출로 분류한다. 별도 AST 호출인 `",".join(...).strip()` 바깥쪽 `strip` 1개는 아직 `UNKNOWN`이다.

**검증:** 전체 단위 테스트 **104개 통과**. Sublist3r와 Click 저장소에 `verify_v24.py` (구조, 출처, 타입 힌트, AST 위치) 통과. Sublist3r의 **기존 Call Graph 간선 원본/수정본 동일**.

## 제약

- `s = ","; s.join(xs)`처럼 수신 객체가 **변수인 일반적인 문자열 메서드**는 여전히 `UNKNOWN`으로 남을 수 있다. 이름만 `join`이라고 문자열이라고 가정하지 않는다.
- `obj.join()`의 객체 타입이 불명확하거나, 표준 라이브러리 import가 재바인딩되는 경우 출처를 확정하지 않는다.
- 유한한 생성자 후보·상속 관계로 판정한 `STDLIB`는 **정적 근거 기반 추론**이다. 전역 monkeypatch, 동적 클래스 속성 변경, 런타임 디스패치의 완전성은 보증하지 않는다.
- 검증은 분석기의 **구조적 일관성**과 대상 원본의 호출 위치 등을 확인한다. 이 수치 자체가 전체 프로젝트의 Precision/Recall/정확도를 증명하지 않는다.

## 실행 (PowerShell)

```powershell
python -m unittest discover -s tests -v
python main.py "C:\path\to\Sublist3r" --output-dir output_sublist3r
python verify_v24.py --graph output_sublist3r\graph_result.json --symbols output_sublist3r\symbol_result.json --repo "C:\path\to\Sublist3r"
```

`output_sublist3r/call_statuses.csv`에서 `join` 검색, `output_sublist3r/report.html`에서 상태를 확인한다.
