# HUD Release 의존성 설치 지연과 커밋별 재게시

대상: `f1b7460121476fedf37a2181b304d5e874d507e5`의 NEXO push 실행
[44331827188](https://github.com/fallen8angel/NEXOdriveXPlus/actions/runs/44331827188).
7000은 최신 NEXO HEAD와 정확히 일치하는 검증 Release가 없으면 배포 대기를 표시한다.
PR의 성공 패키지는 임시 merge commit `6e22b2faad14a051496d57d70d04f79faa7f5b05` 기준으로,
그대로 f1b7460의 이름을 붙여 게시하면 원본 커밋 검증을 위반한다.

## 확인한 원인과 보완

실제 main recovery 로그에서 Python 의존성 설치 이후 Ubuntu apt 다운로드가
10분 넘게 지속된 것을 확인했다. 관련 네이티브 실행에서도 ONNX Runtime 설치는
1초 이내에 끝났지만 apt 패키지 다운로드는 여러 분 소요됐다. GPU 코드·모델 실패의 증거가 아니다.
분석을 위해 지연된 main 실행을 중단했다. 취소 결과를 테스트 실패나 자동 timeout으로 해석하지 않는다.
원래20분 job 예산은 OS 설치와 모든 검증·120초 soak·패키지 빌드를 포함해 여유가 작았다.

HUD workflow를 Python/OS/pinned vendor 설치 단계로 분리하고 각각10/25/5분으로 제한했다.
pip 재시도3회·요청 timeout30초, apt 재시도3회·HTTP/HTTPS idle timeout30초를 사용한다.
job 총 예산은45분이다. 설치 실패는 여전히 실패하며 테스트/빌드/게시를 통과 처리하지 않는다.
원본 모델·HUD 렌더링·차량 제어·7000 설치 안전 조건과 최신 커밋 검증은 수정하지 않는다.

## 정확한 기존 커밋의 검증 재배포

새 NEXO workflow에서 다음과 같이 명시적으로 요청한다.

```sh
gh workflow run hud-recovery.yaml --repo fallen8angel/NEXOdriveXPlus --ref NEXO \
  -f source_commit=f1b7460121476fedf37a2181b304d5e874d507e5
```

입력은40자리 소문자 SHA만 허용한다. 체크아웃된 대상이 현재 origin/NEXO의 조상인지
Git으로 확인하며 미반영 branch/tag/임의 코드는 게시할 수 없다. 빈 입력은 현재 커밋을 사용한다.
기존 모든 모델·회귀·실제 격리 LTE·framebuffer·120초 전송·추출 import 검증을 다시 실행한다.
검증 실패/취소 시 publish는 실행되지 않는다. PR에서도 publish는 실행되지 않는다.

Release 태그와 target_commitish는 workflow 수정 커밋이 아니라 **실제로 검증한 checkout SHA**다.
외부 ZIP SHA256 외에도 JSON metadata, bundle SHA256, tar의 SOURCE_COMMIT이
모두 그 SHA와 일치해야 게시할 수 있다. 기존 immutable Release를 강제 교체하지 않는다.

수정된 최신 NEXO 커밋도 push workflow로 독립적으로 검증·게시한다.
7000은 현재 HEAD의 Release를 선택하며, 이전 f1 Release를 최신 HEAD인 것처럼 표시하지 않는다.
차량에 자동 설치하지 않는다. 배포 인식(API/소프트웨어 검사)과 실제 장치 화면 확인은 구분한다.
