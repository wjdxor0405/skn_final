# Deal Ledger MMVP — 백엔드

목업(`deal_ledger_mockup.html`) 3개 화면과 1:1로 연결되는 규칙 기반 협상 백엔드입니다.

## 구조 (MMVP L0~L4와 대응)

```
app/
  schemas.py    # L0 — 품목/메시지 타입/Envelope/요약 어댑터 인터페이스
  store.py      # L1 — SQLite(카탈로그·거래) + 거래별 텍스트 로그 파일(data/logs/{txid}.jsonl)
  agents.py     # L2 — 규칙 기반 셀러/바이어 판단 로직 (LLM 없음)
  negotiate.py  # L3 — 매칭·협상 진행, 라운드 상한(MAX_ROUNDS=3)으로 무한루프 방지
  report.py     # L4 — 로그에서만 파생되는 자연어 요약 + 리포트 파일 생성
  main.py       # 목업 화면과 연결되는 API 엔드포인트
```

## 실행 방법

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

서버가 뜨면 `deal_ledger_mockup.html`을 브라우저로 그냥 열면 됩니다(로컬 서버 불필요, `fetch`가 `http://127.0.0.1:8000`을 호출).

## 화면 ↔ API 매핑

| 화면 | 동작 | API |
|---|---|---|
| 1. 셀러 등록 | "등록" 클릭 | `POST /api/sellers/register` |
| 2. 구매 요청 등록 | "요청 등록" 클릭 → 즉시 협상까지 실행되고 3번 화면으로 자동 이동 | `POST /api/buyers/request` |
| 3. 협상 결과·승인 | 결과 자동 표시, "승인/거절" 클릭 | `GET /api/deals/{txid}`, `POST /api/deals/{txid}/approve|reject` |

## 데모 시연 순서

1. 셀러 A, 셀러 B를 각각 다른 조건으로 1번 화면에서 등록 (예: A=41원, B=45원/최저38원)
2. 2번 화면에서 구매 요청 등록 (예: 500개, 상한가 42원)
3. 자동으로 3번 화면으로 이동 — 실제 라운드별 OFFER/REJECT/ACCEPT 로그와 자연어 요약이 채워짐
4. 승인 클릭 → `reports/{txid}.txt` 파일 생성 확인

## 이미 반영된 리뷰 사항

- **협상 라운드 상한**: `negotiate.py`의 `MAX_ROUNDS = 3` — 상한가와 최저수용가가 안 맞는 경우 무한 루프 대신 `FAILED`로 결렬 처리
- **동률 처리 정확성**: 자연어 요약에서 동일 가격일 때 "낮은 단가"가 아니라 "동일한 단가지만 먼저 접수된 조건"으로 정확히 서술
- **리포트는 로그에서만 파생**: 에이전트가 리포트를 직접 쓰지 않고, `report.py`가 항상 append된 로그를 읽어서만 생성

## LLM으로 교체하는 방법 (L4 다음 단계)

`report.py`의 `TemplateSummarizer`가 `SummarizerPort`(schemas.py) 인터페이스를 구현하고 있습니다.
같은 인터페이스로 `LLMSummarizer`를 새로 만들고, `main.py`에서 주입하는 인스턴스만 바꾸면 됩니다.
메시지 형식(Envelope)과 API 엔드포인트는 그대로 유지됩니다.

## 자체 호스팅 프로세스 분리가 필요해지면

지금은 4개 컴포넌트(중앙서버·셀러2·바이어)가 한 프로세스 안에서 함수 호출로 통신합니다.
나중에 진짜 별도 프로세스/에이전트로 쪼개고 싶다면, `negotiate.py`의 각 `store.append(...)` 호출 부분을
실제 HTTP 요청이나 메시지 큐 발행으로 바꾸면 됩니다 — 메시지 형식(Envelope)이 이미 고정되어 있어
호출 방식만 바뀌고 나머지 로직은 그대로 재사용됩니다.

## LLM 에이전트로 전환하는 방법 (OpenAI API)

기본값은 규칙 기반(`rule`)입니다. LLM 기반 협상으로 바꾸려면 **`.env` 파일**을 쓰세요.

```bash
# Windows PowerShell이면: copy .env.example .env
```

`.env` 파일을 열어서 값을 채워넣으세요:
```
OPENAI_API_KEY=sk-여기에-발급받은-실제-키
NEGOTIATOR_MODE=llm
```

그다음 평소처럼 그냥 실행하면 됩니다 (별도로 `export`나 `$env:` 명령어 칠 필요 없음):
```bash
uvicorn app.main:app --reload --port 8000
```

**왜 `.env`로 바꿨는지**: `export`는 macOS/Linux(bash) 문법이라 **Windows PowerShell에서는 안 먹혀요** (`$env:OPENAI_API_KEY=...`를 따로 써야 함). `.env` 파일은 OS·셸 종류와 무관하게 코드가 알아서 읽어오기 때문에, 이 문제 자체가 없어집니다. 다시 `rule` 모드로 돌아가려면 `.env`의 `NEGOTIATOR_MODE=rule`로 바꾸거나 그 줄을 지우면 됩니다(기본값이 `rule`이라).

**주의**: `.env` 파일은 실제 API 키가 들어있으니 **절대 git에 커밋하지 마세요** (`.gitignore`에 `.env` 추가 권장, `.env.example`만 커밋).

### 추가된 파일
- `agents.py` — `RuleBasedSellerAgent`/`RuleBasedBuyerAgent` (기존 로직을 클래스로 감쌈)
- `llm_agents.py` — `OpenAISellerAgent`/`OpenAIBuyerAgent` (OpenAI `gpt-4o-mini`, Structured Outputs로 `{price, message}` / `{accept, message}` 형식 강제)
- `audit.py` — **감사자**: LLM이 최저수용가 밑으로 부르거나, 상한가 초과인데 실수로 승인하면 규칙으로 보정. 개입한 경우 로그의 `audit_note` 필드에 사유가 남음

### 정보 은닉 관련 (현재 스코프)
지금은 데모 단순화를 위해 REJECT 메시지에 상대의 정확한 상한가/최저가가 그대로 노출됩니다.
실제 서비스라면 이 부분을 "방향성 힌트"로만 바꿔야 하지만, 이번 단계에서는 의도적으로 보류했습니다.

## 알려진 제약 (MVP 스코프)

- 협상은 동기(synchronous) 처리 — 실제 별도 프로세스라면 비동기 메시지 큐가 필요
- 카탈로그·거래는 SQLite(`data/mmvp.db`)에 저장되어 서버 재시작에도 유지됨, 원문 로그는 거래별 텍스트 파일
- 인증/권한 관리 없음 — 공모전 시연 범위에서 우선순위 낮음으로 확인된 부분
- LLM 모드 사용 시 정보 은닉 미적용 (위 "정보 은닉 관련" 참고)