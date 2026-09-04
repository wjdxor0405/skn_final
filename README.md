# Deal Ledger MMVP — 백엔드

목업 화면과 1:1로 연결되는 규칙 기반 협상 백엔드입니다.
카탈로그를 **Odoo(오픈소스 ERP/CRM)** 에서 읽고, 낙찰 결과를 Odoo 발주서로 되돌려 쓸 수 있습니다.

## 구조 (MMVP L0~L4와 대응)

```
app/
  schemas.py    # L0 — 품목/메시지 타입/Envelope/요약 어댑터 인터페이스
  store.py      # L1 — SQLite(카탈로그·거래) + 거래별 텍스트 로그 파일(data/logs/{txid}.jsonl)
  agents.py     # L2 — 규칙 기반 셀러/바이어 판단 로직 (LLM 없음)
  negotiate.py  # L3 — 매칭·협상 진행, 라운드 상한(MAX_ROUNDS=3)으로 무한루프 방지
  report.py     # L4 — 로그에서만 파생되는 자연어 요약 + 리포트 파일 생성
  main.py       # 목업 화면과 연결되는 API 엔드포인트

  odoo_client.py  # Odoo JSON-RPC 클라이언트 (표준 라이브러리만, 의존성 없음)
  feasibility.py  # 납품 가능성 검증 — 재고·BOM·조달납기·제조 리드타임을 룰로만 판정
  intake.py       # 주문 접수 — 표준계약 범위면 협상 없이, 벗어나면 협상
scripts/
  seed_odoo_demo.py  # Odoo에 제조업 데모 데이터를 넣는 스크립트 (멱등)
```

## 카탈로그 출처 — `CATALOG_SOURCE`

| 값 | 카탈로그 주인 | 셀러 등록 | 낙찰 결과 |
|---|---|---|---|
| `sqlite` (기본) | 이 서비스 | 1번 화면에서 등록 | SQLite에만 기록 |
| `odoo` | **Odoo** | Odoo에서 등록 (1번 화면은 409) | **Odoo 발주서 초안** + chatter에 협상 로그 |
| `snapshot` | **Nexar(Octopart) API** | 불가 (1번 화면은 409) | SQLite에만 기록 (외부 ERP가 없어 발주서 없음) |

`odoo` 모드에서 읽고 쓰는 Odoo 모델:

```
읽기  product.product        품목·우리 재고
      product.supplierinfo   공급처별 단가 · 최소주문량(MOQ) · 납기
      mrp.bom(.line)         자재명세서 · 제조 리드타임
      res.partner            공급처 · 고객사
      ir.config_parameter    고객사별 표준계약 (hodu.contract.<고객ref>.<품목코드>)
쓰기  purchase.order         낙찰 결과를 초안으로 생성 (origin = 거래ID)
      해당 발주서의 chatter   협상 라운드 전문
      button_confirm/cancel  승인/거절 반영
```

## `snapshot` 모드 — 실제 유통 데이터

`sqlite`·`odoo` 모드의 단가·재고·납기는 시연용으로 지어낸 값입니다.
`snapshot` 모드는 **Nexar(Octopart) API에서 받은 실제 전자부품 유통 데이터**를 씁니다.
한 부품에 여러 유통사의 오퍼가 붙기 때문에, 복수 후보 비교라는 이 프로젝트의
핵심 장면이 실데이터로 성립합니다.

**API 키 없이 실행됩니다.** 라이브 호출이 아니라 응답을 떠서 커밋한 JSON을 읽습니다.

```bash
# .env 에 CATALOG_SOURCE=snapshot 만 넣으면 끝입니다
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

## 실행 방법

```bash
pip install -r requirements.txt
cp env.example .env          # Windows: copy env.example .env
uvicorn app.main:app --reload --port 8000
```

브라우저로 `http://127.0.0.1:8000/` 을 열면 목업 화면이 뜹니다(백엔드가 직접 서빙합니다).

`odoo` 모드로 쓰려면 Odoo를 먼저 띄우고 `.env`에 `CATALOG_SOURCE=odoo` 를 넣은 뒤,
데모 데이터를 한 번 넣어줍니다:

```bash
python scripts/seed_odoo_demo.py
```

## 화면 ↔ API 매핑

| 화면 | 동작 | API |
|---|---|---|
| 0. 고객 주문 접수 | "주문 접수" / "납품 가능성만 검증" 클릭 | `POST /api/orders/intake`, `POST /api/feasibility` |
| 1. 셀러 등록 | "등록" 클릭 (`odoo` 모드에서는 사용 안 함) | `POST /api/sellers/register` |
| 2. 구매 요청 등록 | "요청 등록" 클릭 → 즉시 협상까지 실행되고 3번 화면으로 자동 이동 | `POST /api/buyers/request` |
| 3. 협상 결과·승인 | 결과 자동 표시, "승인/거절" 클릭 | `GET /api/deals/{txid}`, `POST /api/deals/{txid}/approve|reject` |

## 데모 시연 순서

### `sqlite` 모드 (기본)

1. 셀러 A, 셀러 B를 각각 다른 조건으로 1번 화면에서 등록 (예: A=41원, B=45원/최저38원)
2. 2번 화면에서 구매 요청 등록 (예: 500개, 상한가 42원)
3. 자동으로 3번 화면으로 이동 — 실제 라운드별 OFFER/REJECT/ACCEPT 로그와 자연어 요약이 채워짐
4. 승인 클릭 → `reports/{txid}.txt` 파일 생성 확인

### `odoo` 모드

셀러 등록 단계가 없습니다 — 카탈로그의 주인이 Odoo입니다.

1. 0번 화면에서 **대한AI연구원 / `FG-GPUSRV-001` / 60대 / 90일** → **주문 접수**
   → `표준계약 범위 — 협상 없이 자동 처리`. 발주서 chatter에 로그가 4줄뿐입니다
2. 같은 고객·같은 품목으로 **300대** → **주문 접수**
   → `예외 발주 — 에이전트 협상`. 이번엔 협상 라운드가 붙습니다
3. `FG-GPUSRV-001` / 100대 / **10일** → **납품 가능성만 검증** → 왜 불가능한지 근거가 나옴
   (GPU 재고 10장뿐이라 90장 부족, 조달 7일 + 제조 5일 + 생산 10일 = 22일 필요)
3. 결과의 `P000xx` 링크를 열면 Odoo 발주서 — **chatter에 협상 전 과정**이 남아 있음
4. 2·3번 화면으로 개별 품목 협상도 가능. 승인/거절하면 Odoo 발주서가 확정/취소됨

### `snapshot` 모드

셀러 등록 단계가 없습니다 — 카탈로그의 주인이 외부(Nexar/Octopart)입니다.

1. 2번 화면에서 `STM32F103C8T6` / **500개** / 상한가 **6,000원** / 납기 60일
2. 3번 화면에 실제 유통사 9곳이 후보로 뜹니다 — Arrow 3,608원 · Verical 3,608원 ·
   Future 4,851원 · Newark 6,446원 · TME 8,865원 · DigiKey/Mouser 11,135원 · Avnet 11,162원
3. 재고가 없는 곳은 스크리닝에서 사유와 함께 떨어집니다
   (`재고부족(보유 0 < 요청 500)` — Newark · RS · Avnet)
4. 낙찰은 Arrow Electronics @ 3,608원. 승인하면 `reports/{txid}.txt` 생성

품목 28종 전부 인가 유통사가 3곳 이상이라 어떤 품목을 골라도 후보 비교가 됩니다.

**수량을 바꾸면 이기는 유통사가 바뀝니다.** 요청 수량에 맞는 가격구간이 적용되기
때문입니다. `39-01-2040`(Molex 전원 커넥터) / 상한가 200원으로 수량만 바꿔보면:

| 요청 | 낙찰 | 단가 |
|---|---|---|
| 10개 | Heilind Americas | 154원 |
| 1,000개 | TME | 120원 |
| 10,000개 | Verical | 68원 |

28품목 중 22품목에서 수량에 따라 최저가 유통사가 바뀝니다.

## 표준계약 — 에이전트가 언제 나서지 않는가

고객사별로 "분기당 몇 대가 정상인가"를 Odoo에 설정해 둡니다. 이 범위 안이면 **협상 라운드가
아예 돌지 않고**, 직전에 거래한 공급처의 현재 단가로 그대로 발주합니다.

| 고객사 | 분기 표준 | 허용배수 | 표준 상한 |
|---|---|---|---|
| 대한AI연구원 | 60대 | 1.2 | 72대 |
| 누리클라우드 | 40대 | 1.2 | 48대 |
| 세빛테크놀로지 | — | — | 표준계약 없음 → 항상 예외(신규 거래처) |

```
대한AI연구원 60대   → STANDARD   협상 없음. 로그 4줄 (REQUEST·OFFER·ACCEPT·SETTLED)
대한AI연구원 300대  → EXCEPTION  협상 가동. 부족 자재 3종에 각각 라운드가 붙음
```

표준 범위인데 기존 공급처가 지금 조건(MOQ·납기)을 못 맞추면 협상으로 되돌아가고,
그 사실이 근거에 남습니다.

**납기가 낙찰을 가르는 장면** — 2번 화면에서 `RM-GPU-H100` 90개, 상한가 42,000,000원으로
납기만 바꿔 두 번 요청해 보세요.

| 요구 납기 | 낙찰 | 단가 | 이유 |
|---|---|---|---|
| 10일 | 바이어드 | 41,000,000원 | 7일 납기로 유일하게 적격 — **가장 비싼 곳에서 사게 됨** |
| 30일 | 오퍼렛 | 36,500,000원 | 셋 다 적격이 되어 최저가가 이김 |

90개 기준 4억 5백만원 차이입니다. 단순 최저가 정렬로는 나오지 않는 판단입니다.

Odoo에서 공급처 단가·MOQ·납기를 바꾸면(Purchase > Products > 품목 > Purchase 탭)
**코드 변경 없이 다음 협상부터 낙찰 결과가 달라집니다.**

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
OPENAI_MODEL=gpt-4o-mini   # 생략 가능. 다른 모델 쓰려면 이 값만 바꾸면 됨 (예: gpt-4o, gpt-4.1-mini)
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
- `snapshot` 모드는 발주서를 만들지 않습니다 — 외부 ERP가 없습니다. BOM 전개(`feasibility.py`)도 Odoo 담당이라 이 모드에서는 `/api/feasibility` 를 쓸 수 없습니다
- `snapshot` 모드에서 납기 스크리닝이 사실상 걸리지 않습니다 — 인가 유통사는 재고가 있으면 납기가 0이고, 재고가 없으면 납기보다 재고부족으로 먼저 떨어지기 때문입니다
- `llm` 모드에서 유보가격 액수가 자유 문장으로 새어나갈 여지 — 프롬프트로 금지했으나 출력 검사는 없음 (위 "정보 은닉" 절 참고). `rule` 모드는 해당 없음
## 라이선스와 오픈소스 사용 고지

이 저장소의 코드는 **MIT 라이선스**입니다([`LICENSE`](LICENSE)).

**Odoo (Community 판, LGPLv3)** 를 CRM·ERP로 함께 씁니다. 다만 **이 저장소에는 Odoo
코드가 포함되어 있지 않습니다** — `app/odoo_client.py`가 Odoo가 기본 제공하는
**JSON-RPC External API**를 표준 라이브러리(urllib)로 호출할 뿐이고, Odoo 모듈을
작성하거나 Odoo 코드를 링크하지 않습니다. 그래서 LGPL의 조건이 이 코드로 전파되지
않고, 고지 의무만 남습니다. Odoo를 이 서비스와 함께 **배포**하게 되면(예: 컨테이너
이미지에 Odoo를 같이 담는 경우) LGPLv3 조건을 다시 확인해야 합니다.

주요 파이썬 의존성과 라이선스:

| 패키지 | 라이선스 |
|---|---|
| FastAPI, Pydantic, SQLAlchemy | MIT |
| Starlette, Uvicorn, python-dotenv | BSD-3-Clause |
| openai (`NEGOTIATOR_MODE=llm` 에서만 사용) | Apache-2.0 |

전체 목록은 [`requirements.txt`](requirements.txt)에 있습니다.
