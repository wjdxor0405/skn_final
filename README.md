# Deal Ledger MMVP — 백엔드

목업 화면과 1:1로 연결되는 규칙 기반 협상 백엔드입니다.
카탈로그는 **실제 전자부품 유통 데이터**(Nexar/Octopart 응답 스냅샷)에서 읽습니다.
API 키 없이 그대로 실행됩니다.

> **2026-09-07 — 방향이 바뀌었습니다.** 9/5~9/6 회의에서 제조업 도메인과 CRM(Odoo)이
> 폐기됐습니다. 무엇이 왜 걷혔고 무엇이 아직 안 갈렸는지는
> [`docs/wiki_대조_20260907.md`](docs/wiki_대조_20260907.md)에 있습니다.

## 구조 (MMVP L0~L4와 대응)

```
app/
  schemas.py    # L0 — 품목/메시지 타입/Envelope/요약 어댑터 인터페이스
  store.py      # L1 — SQLite(카탈로그·거래) + 거래별 텍스트 로그 파일(data/logs/{txid}.jsonl)
  agents.py     # L2 — 규칙 기반 셀러/바이어 판단 로직 (LLM 없음)
  negotiate.py  # L3 — 매칭·협상 진행, 라운드 상한(MAX_ROUNDS=3)으로 무한루프 방지
  report.py     # L4 — 로그에서만 파생되는 자연어 요약 + 리포트 파일 생성
  main.py       # 목업 화면과 연결되는 API 엔드포인트

  snapshot_catalog.py  # 커밋된 Nexar 응답을 카탈로그로 읽는 로더 (환산·가정이 전부 여기)
  feasibility.py       # 납품 가능성 검증 — 재고·최소주문량·조달납기를 룰로만 판정
scripts/
  nexar_snapshot.py    # Nexar 응답 원본을 그대로 뜨는 조회 스크립트 (평소엔 안 씁니다)
```

## 카탈로그 출처 — `CATALOG_SOURCE`

| 값 | 카탈로그 주인 | 셀러 등록 |
|---|---|---|
| `snapshot` (기본) | **Nexar(Octopart) API** | 불가 (1번 화면은 409) |
| `sqlite` | 이 서비스 | 1번 화면에서 등록 |

`snapshot` 이 기본값이라 **아무것도 설정하지 않아도 실데이터로 돕니다.**
`sqlite` 는 셀러가 직접 카탈로그를 올리는 경로가 필요할 때 씁니다.

## `snapshot` 모드 — 실제 유통 데이터

`sqlite` 모드의 단가·재고·납기는 시연용으로 지어낸 값입니다.
`snapshot` 모드는 **Nexar(Octopart) API에서 받은 실제 전자부품 유통 데이터**를 씁니다.
한 부품에 여러 유통사의 오퍼가 붙기 때문에, 복수 후보 비교라는 이 프로젝트의
핵심 장면이 실데이터로 성립합니다.

**API 키 없이 실행됩니다.** 라이브 호출이 아니라 응답을 떠서 커밋한 JSON을 읽습니다.

```bash
uvicorn app.main:app --reload --port 8000
```

### 무엇이 실데이터이고 무엇이 합성인가

이 경계가 흐려지면 시연의 의미가 사라지므로 명시합니다.

| 값 | 출처 |
|---|---|
| 단가 · 수량별 가격구간 | **API 원본** |
| 재고(`inventoryLevel`) · 최소주문량(`moq`) | **API 원본** |
| 공장 리드타임(`factoryLeadDays`) | **API 원본** |
| 판매자 · 인가 여부 · 제조사 · 사양 | **API 원본** |
| 원화 환산 | **API가 계산한 환율** — 판매자마다 원 통화가 다릅니다 (USD 1,351.30 · EUR 1,571.33 · HKD 172.35 · CNY 201.30 · JPY 8.65 · GBP 1,830.53) |
| **`floor_price` (셀러 최저 수용가)** | **합성** — 제시가 × `SNAPSHOT_FLOOR_RATIO`(기본 0.90) |

합성값은 `floor_price` 하나뿐입니다. 셀러가 얼마까지 받아들이는지는 **어떤 공개
데이터에도 없습니다** — 공개되면 협상이 성립하지 않는 값이기 때문입니다.

중요한 것은 **스냅샷 파일(`data/nexar_snapshot.json`)에는 합성값이 하나도 없다**는
점입니다. 그 파일은 API 응답 원본과 조회 이력(보낸 쿼리 전문 포함)뿐이고,
`floor_price` 합성·통화 환산·수량구간 선택은 전부 `app/snapshot_catalog.py` 가
**읽는 시점에** 합니다. 가정이 바뀌면 로더만 고치면 되고 재조회가 필요 없습니다.

### 조정할 수 있는 것 (`env.example` 참고)

| 환경변수 | 기본 | 뜻 |
|---|---|---|
| `SNAPSHOT_FLOOR_RATIO` | `0.90` | 유일한 합성값의 크기 = 협상 여지 |
| `SNAPSHOT_AUTHORIZED_ONLY` | `true` | 인가 유통사만 후보로. 끄면 판매자가 33곳 → 96곳 |
| `SNAPSHOT_STOCK_LEAD_DAYS` | `0` | 재고에서 나가는 물량의 납기 |
| `SNAPSHOT_CURRENCY` / `SNAPSHOT_FX` | `KRW` / `USD=1400` | 환산 통화와 폴백 환율 |

`SNAPSHOT_FX` 는 폴백일 뿐 실제로는 쓰이지 않습니다. 조회할 때 통화를 넘겨두면
응답에 API가 계산한 환산값이 실려 오고, 현재 스냅샷은 **1,039행 전부** 그 값을
씁니다. 어느 단가가 API 값이고 어느 것이 폴백인지는 `snapshot_catalog.diagnostics()`
의 `price_sources` 로 확인할 수 있습니다.

`SNAPSHOT_AUTHORIZED_ONLY` 를 켜두는 이유는 비인가 유통사의 최저가가 인가 유통사의
1/4.4(중앙값)이고, 극단은 기가비트 이더넷 PHY 37원 · 파워 MOSFET 13원처럼 같은
물건이라고 보기 어려운 값이기 때문입니다. 스냅샷 파일에는 전부 남아 있으므로
끄면 즉시 되돌아옵니다.

### 스냅샷을 다시 뜨려면 (평소에는 필요 없습니다)

무료 플랜의 파트 한도는 **조회한 파트 수**로 차감됩니다. 한 번 제대로 돌리고
결과를 커밋하는 방식이라, 아래는 데이터를 갱신할 때만 씁니다.

```bash
# .env 에 NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET 추가 후
pip install requests                                    # 조회할 때만 필요

python scripts/nexar_snapshot.py --introspect SupOffer  # 한도 차감 없음
python scripts/nexar_snapshot.py --print-query          # 한도 차감 없음
python scripts/nexar_snapshot.py --mpn-file scripts/mpns.txt --out data/nexar_snapshot.json
```

조회할 MPN 목록과 고른 기준은 `scripts/mpns.txt` 에 있습니다.

### 더 읽을 것

| 문서 | 무엇 |
|---|---|
| `docs/스냅샷-데이터-구조.md` | 파일 안에 무엇이 들어 있는가, 어떤 필드가 얼마나 비어 있는가 |
| `docs/decisions/0002-…` | 왜 원본을 그대로 뜨고 해석을 읽는 시점으로 미뤘는가 |
| `docs/decisions/0003-…` | 왜 인가 유통사만 후보로 두는가 |
| `docs/decisions/0004-…` | 작업지시서 범위 밖에서 고친 두 파일 — **병합 담당자가 읽어야 합니다** |
| `docs/decisions/0005-…` | 왜 Odoo·제조업을 걷어냈고 무엇을 남겼는가 |
| `docs/wiki_대조_20260907.md` | 9/5~9/6 회의가 뒤집은 것과 아직 안 갈린 쟁점 4개 |
| `docs/해커톤_요건_대조.md` | 제출 요건 대비 상태 — Strands 진행도가 여기 있습니다 |

## 실행 방법

```bash
pip install -r requirements.txt
cp env.example .env          # Windows: copy env.example .env
uvicorn app.main:app --reload --port 8000
```

브라우저로 `http://127.0.0.1:8000/` 을 열면 목업 화면이 뜹니다(백엔드가 직접 서빙합니다).
`.env` 없이도 그대로 돕니다 — 기본값이 `snapshot` 이고 스냅샷은 커밋돼 있습니다.

## 화면 ↔ API 매핑

| 화면 | 동작 | API |
|---|---|---|
| 0. 납품 가능성 검증 | "납품 가능성 검증" / "부족분 조달까지" 클릭 | `POST /api/feasibility`, `POST /api/feasibility/procure` |
| 1. 셀러 등록 | "등록" 클릭 (`snapshot` 모드에서는 409) | `POST /api/sellers/register` |
| 2. 구매 요청 등록 | "요청 등록" 클릭 → 즉시 협상까지 실행되고 3번 화면으로 자동 이동 | `POST /api/buyers/request` |
| 3. 협상 결과·승인 | 결과 자동 표시, "승인/거절" 클릭 | `GET /api/deals/{txid}`, `POST /api/deals/{txid}/approve|reject` |

## 데모 시연 순서

### `snapshot` 모드 (기본)

셀러 등록 단계가 없습니다 — 카탈로그의 주인이 외부(Nexar/Octopart)입니다.

1. 2번 화면에서 `STM32F103C8T6` / **500개** / 상한가 **6,000원** / 납기 60일
2. 3번 화면에 실제 유통사 9곳이 후보로 뜹니다 — Arrow 3,608원 · Verical 3,608원 ·
   Future 4,851원 · Newark 6,446원 · TME 8,865원 · DigiKey/Mouser 11,135원 · Avnet 11,162원
3. 재고가 없는 곳은 스크리닝에서 사유와 함께 떨어집니다
   (`재고부족(보유 0 < 요청 500)` — Newark · RS · Avnet)
4. 낙찰은 Arrow Electronics @ 3,608원. 승인하면 `reports/{txid}.txt` 생성

품목 28종 전부 인가 유통사가 3곳 이상이라 어떤 품목을 골라도 후보 비교가 됩니다.

0번 화면의 납품 가능성 검증 — `39-01-2040` / 30일로 수량을 올려보면:

| 요청 | 판정 | 근거 |
|---|---|---|
| 5,000개 | 가능 | 조달 0일 (재고 출하) |
| 838,293개 | **불가** | 그만큼 댈 수 있는 판매자가 없습니다 (한 곳의 최대 보유량 694,443개) |

"부족분 조달까지"를 누르면 검증에서 협상까지 그대로 이어집니다.

**수량을 바꾸면 이기는 유통사가 바뀝니다.** 요청 수량에 맞는 가격구간이 적용되기
때문입니다. `39-01-2040`(Molex 전원 커넥터) / 상한가 200원으로 수량만 바꿔보면:

| 요청 | 낙찰 | 단가 |
|---|---|---|
| 10개 | Heilind Americas | 154원 |
| 1,000개 | TME | 120원 |
| 10,000개 | Verical | 68원 |

28품목 중 22품목에서 수량에 따라 최저가 유통사가 바뀝니다.
**단순 최저가 정렬로는 나오지 않는 판단입니다** — 재고·최소주문량·납기가 먼저 후보를 거릅니다.

### `sqlite` 모드

카탈로그를 이 서비스가 직접 들고 갑니다. `.env` 에 `CATALOG_SOURCE=sqlite` 를 넣으세요.

1. 셀러 A, 셀러 B를 각각 다른 조건으로 1번 화면에서 등록 (예: A=41원, B=45원/최저38원)
2. 2번 화면에서 구매 요청 등록 (예: 500개, 상한가 42원)
3. 자동으로 3번 화면으로 이동 — 실제 라운드별 OFFER/REJECT/ACCEPT 로그와 자연어 요약이 채워짐
4. 승인 클릭 → `reports/{txid}.txt` 파일 생성 확인

## 걷어낸 것 — 표준계약 분기와 Odoo 연동

`CATALOG_SOURCE=odoo` 경로와 표준계약 기반 예외 협상(`app/intake.py`)이 있었습니다.
고객사별 분기 표준수량을 두고 **그 범위 안이면 협상 라운드가 아예 돌지 않는** 분기였고,
"에이전트가 왜 필요한가"에 답하는 자리였습니다.

9/6 회의에서 CRM 폐기가 확정되면서 함께 내려왔습니다 — 기준값이 Odoo에 있었기
때문입니다. **아이디어까지 버린 것은 아닙니다.** 기준값을 어디에 둘지는 남은 쟁점
하나(협상이 메인 테마인가)에 걸려 있어서, 그게 갈리면 되살립니다. 자세한 것은
[`docs/decisions/0005-…`](docs/decisions/0005-odoo-와-제조업-도메인을-걷어낸다.md).

## 이미 반영된 리뷰 사항

- **협상 라운드 상한**: `negotiate.py`의 `MAX_ROUNDS = 3` — 상한가와 최저수용가가 안 맞는 경우 무한 루프 대신 `FAILED`로 결렬 처리
- **동률 처리 정확성**: 자연어 요약에서 동일 가격일 때 "낮은 단가"가 아니라 "동일한 단가지만 먼저 접수된 조건"으로 정확히 서술
- **리포트는 로그에서만 파생**: 에이전트가 리포트를 직접 쓰지 않고, `report.py`가 항상 append된 로그를 읽어서만 생성

## 리포트 요약을 LLM으로 (L4 다음 단계) — 완료

✅ 했습니다 — `report.py` 의 `LLMSummarizer` 가 `SummarizerPort` 를 구현하고
`SUMMARIZER_MODE=llm` 으로 켜집니다. 메시지 형식(Envelope)과 API 엔드포인트는
그대로입니다. 자세한 것은 아래 "LLM 을 켜는 방법" 절.

## 자체 호스팅 프로세스 분리가 필요해지면

지금은 4개 컴포넌트(중앙서버·셀러2·바이어)가 한 프로세스 안에서 함수 호출로 통신합니다.
나중에 진짜 별도 프로세스/에이전트로 쪼개고 싶다면, `negotiate.py`의 각 `store.append(...)` 호출 부분을
실제 HTTP 요청이나 메시지 큐 발행으로 바꾸면 됩니다 — 메시지 형식(Envelope)이 이미 고정되어 있어
호출 방식만 바뀌고 나머지 로직은 그대로 재사용됩니다.

## LLM 을 켜는 방법 — 두 자리

기본값은 **LLM 없이** 돕니다(`rule` + `template`). 켤 수 있는 자리는 둘입니다.

| 환경변수 | 기본 | 켜면 |
|---|---|---|
| `NEGOTIATOR_MODE` | `rule` | `strands` — **Strands Agents SDK 로 협상**(해커톤 필수 요건)<br>`llm` — OpenAI SDK 직접 호출(구형) |
| `SUMMARIZER_MODE` | `template` | `llm` — **설명 제너레이션**. 아래 |

### 설명 제너레이션 — 왜 XAI 가 아닌가

멘토 피드백의 결론입니다. XAI 는 기각됐습니다 — 팀이 쓰는 건 로컬 모델이 아니라
LLM API 라 어텐션을 볼 수 없고, LIME 은 토큰을 하나씩 빼며 재추론해야 해서
1회 예측에 수천 원까지 갑니다. 대안이 **예측 후에 "왜 이렇게 골랐는지"를 텍스트로
푸는 것**이었습니다. COT(답을 내기 전에 추론을 먼저 생성)와 다릅니다 — 여기서
판단은 이미 룰이 끝냈고 LLM 은 **끝난 판단을 설명만** 합니다.

그래서 **모델이 로그를 직접 읽지 않습니다.** `report.py` 의 `_facts()` 가 룰로
뽑아낸 사실만 넘어갑니다. 숫자와 판정이 전부 룰에서 오므로 지어낼 자리가 없고,
같은 사실을 템플릿도 쓰기 때문에 두 구현이 다른 사실을 말할 수 없습니다.

그래도 모델은 숫자를 흘릴 수 있어서 `_verify()` 가 **낙찰자와 낙찰가가 출력에
실제로 들어 있는지**만 확인합니다. 생성이 실패하든 사실과 어긋나든 템플릿이
대신 나갑니다 — 리포트는 승인 흐름의 산출물이라 없으면 안 됩니다.

### Strands 어댑터

`app/strands_agents.py` 가 `Agent` + `structured_output(pydantic)` 을 씁니다.
`SellerAgentPort`/`BuyerAgentPort` 인터페이스가 같아서 `negotiate.py` 는 분기
한 절만 늘었습니다. **모델 교체는 `_model()` 한 곳**입니다 — Bedrock 으로 가려면
`OpenAIModel` 을 `BedrockModel` 로 바꾸는 것이 전부입니다.

프롬프트는 `app/agent_prompts.py` 에 모아 두 어댑터가 공유합니다. 정보 은닉
규약(셀러는 바이어 상한가를 못 본다)이 프롬프트 **문장** 안에 있어서 타입도
린터도 안 잡아 주고, 어댑터마다 들고 있으면 한쪽만 고쳐질 때 조용히 깨집니다.

> **아직 확인되지 않은 것**: 모델 왕복을 실제로 돌려 보지 못했습니다(API 키 없음).
> 테스트는 프롬프트 규약·가드레일·폴백까지만 덮습니다. 제출 전에 키를 넣고
> 두 모드를 각각 한 번씩 돌려 볼 것.

### 설정 방법

**`.env` 파일**을 쓰세요.

```bash
# Windows PowerShell이면: copy .env.example .env
```

`.env` 파일을 열어서 값을 채워넣으세요:
```
OPENAI_API_KEY=sk-여기에-발급받은-실제-키
NEGOTIATOR_MODE=strands    # 또는 llm
SUMMARIZER_MODE=llm        # 설명 제너레이션도 켜려면
OPENAI_MODEL=gpt-4o-mini   # 생략 가능. 다른 모델 쓰려면 이 값만 바꾸면 됨 (예: gpt-4o, gpt-4.1-mini)
```

그다음 평소처럼 그냥 실행하면 됩니다 (별도로 `export`나 `$env:` 명령어 칠 필요 없음):
```bash
uvicorn app.main:app --reload --port 8000
```

**왜 `.env`로 바꿨는지**: `export`는 macOS/Linux(bash) 문법이라 **Windows PowerShell에서는 안 먹혀요** (`$env:OPENAI_API_KEY=...`를 따로 써야 함). `.env` 파일은 OS·셸 종류와 무관하게 코드가 알아서 읽어오기 때문에, 이 문제 자체가 없어집니다. 다시 `rule` 모드로 돌아가려면 `.env`의 `NEGOTIATOR_MODE=rule`로 바꾸거나 그 줄을 지우면 됩니다(기본값이 `rule`이라).

**주의**: `.env` 파일은 실제 API 키가 들어있으니 **절대 git에 커밋하지 마세요** (`.gitignore`에 `.env` 추가 권장, `.env.example`만 커밋).

### 관련 파일
- `agents.py` — `RuleBasedSellerAgent`/`RuleBasedBuyerAgent` (기본값)
- `strands_agents.py` — `StrandsSellerAgent`/`StrandsBuyerAgent` (Strands SDK)
- `llm_agents.py` — `OpenAISellerAgent`/`OpenAIBuyerAgent` (OpenAI SDK 직접 호출)
- `agent_prompts.py` — 두 LLM 어댑터가 공유하는 프롬프트
- `audit.py` — **감사자**: LLM이 최저수용가 밑으로 부르거나, 상한가 초과인데 실수로 승인하면 규칙으로 보정. 개입한 경우 로그의 `audit_note` 필드에 사유가 남음

### 정보 은닉 — 무엇을 닫았고 무엇이 남았나

**셀러는 바이어의 상한가를 볼 수 없습니다.** 상대의 지불의사 최대치를 알면 거기 붙여
부르면 그만이라 협상이 성립하지 않기 때문에, 인터페이스·엔벨로프·LLM 프롬프트 세 곳에서
전부 빼냈습니다(`5462e43`·`1ade8d9`).

- RFQ가 **접수**(`buyer→central`, 예산 포함)와 **공고**(`central→*`, 예산 제외)로 분리되어 있음
- `SellerAgentPort.decide`는 상한가를 **인자로 받지 않음**. 셀러가 아는 것은 자기
  제시가·최저수용가와, 자기가 제시했다가 거절당한 이력뿐
- REJECT 페이로드는 `{"reason": "상한가 초과", ...}` — 사유만 있고 **금액은 없음**
- 규칙 기반 바이어의 거절 메시지도 액수를 밝히지 않음("제시가가 예산을 초과하여 거절합니다.")
- 리포트가 상한가를 읽는 곳은 **접수** 메시지 — 셀러에게 나가는 경로가 아님

**`llm` 모드의 자유 문장 — 프롬프트로 막았지만 보장은 아닙니다.** 에이전트는 판단하려면
자기 유보가격을 알아야 하므로 프롬프트에는 액수가 들어갑니다(바이어의 상한가, 셀러의
최저수용가). 그 LLM이 상대에게 보낼 메시지에 액수를 적어버릴 수 있어서, **양쪽 프롬프트에
"유보가격 액수는 메시지에 쓰지 말라"는 지시를 넣었습니다**(`llm_agents.py`). 다만 지시일
뿐이고 `audit.py`가 검증하는 것은 가격 판단이라 **메시지 본문은 검사하지 않습니다** —
확실히 막으려면 출력 문장에서 유보가격 액수를 찾아내는 검사를 감사자에 추가해야 합니다.
`rule` 모드(기본값)는 액수를 문장에 넣을 경로 자체가 없습니다.

## 알려진 제약 (MVP 스코프)

- 협상은 동기(synchronous) 처리 — 실제 별도 프로세스라면 비동기 메시지 큐가 필요
- 카탈로그·거래는 SQLite(`data/mmvp.db`)에 저장되어 서버 재시작에도 유지됨, 원문 로그는 거래별 텍스트 파일
- 인증/권한 관리 없음 — 공모전 시연 범위에서 우선순위 낮음으로 확인된 부분
- 외부 ERP로 결과를 되돌려 쓰지 않습니다 — 낙찰은 SQLite와 `reports/{txid}.txt` 에만 남습니다
- 납품 가능성 검증에 '우리' 재고와 생산 개념이 없습니다 — Octopart에는 둘 다 없어서 모든 품목을 재고 0인 구매 품목으로 봅니다. 조달 납기·단가·공급 한도는 실제 유통사 데이터에서 옵니다
- 납기 스크리닝이 사실상 걸리지 않습니다 — 인가 유통사는 재고가 있으면 납기가 0이고, 재고가 없으면 납기보다 재고부족으로 먼저 떨어지기 때문입니다
- `llm` 모드에서 유보가격 액수가 자유 문장으로 새어나갈 여지 — 프롬프트로 금지했으나 출력 검사는 없음 (위 "정보 은닉" 절 참고). `rule` 모드는 해당 없음
## 라이선스와 오픈소스 사용 고지

이 저장소의 코드는 **MIT 라이선스**입니다([`LICENSE`](LICENSE)).

**데이터 출처 — Nexar(Octopart).** `data/nexar_snapshot.json` 은 Nexar API 응답
원본이고, 카탈로그의 단가·재고·납기가 전부 여기서 옵니다. 무엇이 원본이고 무엇이
합성인지는 위 "무엇이 실데이터이고 무엇이 합성인가" 절에 있습니다. 재배포 조건은
Nexar 이용약관을 따릅니다.

**Odoo (Community 판, LGPLv3)** 를 CRM·ERP로 쓰던 시기가 있었고 히스토리에 남아
있습니다(`app/odoo_client.py`, 커밋 `7228944` 에서 제거). 그때도 **Odoo 코드를 이
저장소에 포함하지 않았습니다** — 기본 제공되는 JSON-RPC External API를 표준
라이브러리로 호출했을 뿐이라 LGPL 조건이 전파되지 않았습니다. 현재 코드는 Odoo를
전혀 쓰지 않습니다.

주요 파이썬 의존성과 라이선스:

| 패키지 | 라이선스 |
|---|---|
| FastAPI, Pydantic, SQLAlchemy | MIT |
| Starlette, Uvicorn, python-dotenv | BSD-3-Clause |
| openai (`NEGOTIATOR_MODE=llm` 에서만 사용) | Apache-2.0 |

전체 목록은 [`requirements.txt`](requirements.txt)에 있습니다.
