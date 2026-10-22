# KnitCode JSE v2.5 — Git 업로드용 정리본 안내

## 정리 원칙
- 원본 `KnitCode_jse.zip`은 변경하지 않았습니다.
- 모든 `.py` 소스 및 테스트 코드는 **원본과 SHA-256이 동일**합니다. 분석 알고리즘은 수정하지 않았습니다.
- 원본 444개 파일 중 76개를 선별했습니다. 설명 문서인 이 파일을 추가하여 최종 ZIP에는 77개 파일이 들어 있습니다.
- `README.md`는 버전 표기를 v2.5로 통일하고 역사 문서 경로를 갱신했습니다.
- `.gitignore`는 기존 출력 폴더·캐시·검증 산출물이 다시 Git에 들어오지 않도록 보완했습니다.

## 제외한 것
- `KnitCode_3/`: 기존 소스 중복본(194개)
- `output_click_context/`, `output_requests_context/`, `output_sublist3r_context/`: 기존 분석 JSON·CSV·HTML(24개)
- `__pycache__/`, `*.pyc`: 재생성 가능한 캐시(36개)
- `validation/`의 과거 로그, JSON/CSV/HTML, 벤치마크·바이너리 스냅샷(103개)
- 루트의 과거 패치 파일, 로그 및 벤치마크 산출물(11개)

제외한 항목은 **원본 ZIP에 그대로 남아 있습니다.** 사용자 PC의 파일을 삭제하지 않았습니다.

## 남긴 것
- 정적 분석 핵심 모듈(`main.py`, `graph_builder.py`, `object_flow.py`, `override_relations.py` 등)
- 버전 검증용 Python 스크립트 및 테스트(`tests/`)
- 테스트 fixtures(`tests/fixtures/`, `examples/`, 일부 `validation/` fixtures)
- 사용 문서(`README.md`, `OVERRIDE_RELATIONS_KO.md`, 이전 버전 문서 `docs/history/`)
- 원본 템플릿 JSON(`gold_annotations.template.json`)

## 실제 검증 결과
- 전체 단위 테스트: **279개 실행 / 279개 통과** (Python 3.13.5, 테스트 종료 코드 0)
- CLI 스모크 테스트: `validation/override_fixture` 분석 성공
- 출력 그래프 간선: `CONTAINS` 2, `INHERITS` 1, `CALLS` 2, `INSTANTIATES` 1, `OVERRIDDEN_BY` 1
- `edge_count == len(edges)` 일치 확인

## 사용 명령

```powershell
cd KnitCode_jse_clean
python -m unittest discover -s tests -v
python main.py validation/override_fixture --output-dir output
```

## 알아둘 점
- `validation/override_regression.py replay`는 과거 출력과 바이트 단위로 비교하는 스크립트로, 현재 정리본에서는 원본의 `validation/override_baseline.pkl.gz`를 제외했습니다. 필요할 때 원본 ZIP에서 해당 파일을 가져와야 합니다. pickle 파일은 출처가 검증된 경우에만 사용하세요.
- `docs/history/`의 과거 보고서에 나오는 경로·과거 실행 수치가 정리본에서 그대로 재현되는 것은 아닙니다. 해당 시점의 참고 기록입니다.
- 예전 보고서와 패치(`*.patch`), 큰 실행 산출물은 원본 ZIP을 참조하세요.
- Git 업로드 전에 통합 저장소의 기존 파일과 충돌 여부를 확인하고 **별도 브랜치**에 올리는 것을 권장합니다.
