// 화면 ↔ 백엔드 변환 규칙 테스트. Node 내장 러너(`npm test`)로 돌고 새 의존성이 없다 — mapping.ts 는 타입만 import 하므로
// Node 의 타입 제거(type stripping)로 바로 읽힌다.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  backendSlotFromRowPart, guideStepsFromWire, budgetNoticeFromWire, budgetWarningFromWire, checksFromWire, choicesFromWire, compatChecksFromWire, compatFromWire, contributionFromWire, fieldsFromWire, partExplanation, suggestionFromPlan, wantsPartExplanation, currentSpecsFromRows, gamesFromText, itemFromWire, planFromResult, priorityFromText, purposeFromText, replyTextFromWire, resolutionFromText, reviewRowsFromWire,
  setupFromReport, slotKey, upgradePartsFromText,
} from '../src/api/http/mapping.ts'

const wireItem = (over = {}) => ({
  item_id: 'it-1', slot: 'CPU', slot_label: 'CPU',
  product: { name: 'AMD Ryzen 5 7600', brand: 'AMD', spec_summary: '6코어 · AM5' },
  price: 221750, price_source: 'synthetic', price_observed_at: null, qty: 1, selected: true, timing: 'now',
  budget_share: 0.168, review: null, reason: { status: 'ready', text: '게임 성능 대비 가격이 좋습니다.' },
  alternatives_count: 3, ...over,
})

test('슬롯 이름은 화면 부품 키로 바뀌고, 모르는 슬롯은 null', () => {
  const expected = { CPU: 'cpu', GPU: 'gpu', RAM: 'ram', 메인보드: 'board', 저장장치: 'ssd', 파워: 'psu', 케이스: 'case', 쿨러: 'cooler' }
  for (const [slot, key] of Object.entries(expected)) assert.equal(slotKey(slot), key)
  assert.equal(slotKey('모니터'), null)
})

test('용도 문장 → 백엔드 purpose', () => {
  assert.equal(purposeFromText('150만원으로 QHD 게임용 PC를 맞춰줘'), 'game')
  assert.equal(purposeFromText('영상편집용인데 조용했으면 좋겠어'), 'creation')
  assert.equal(purposeFromText('사무용 PC가 필요해요'), 'office')
  assert.equal(purposeFromText('대학생 공부용'), 'study')
  assert.equal(purposeFromText('그냥 PC 추천해줘'), 'other')
})

test('소음 선택지·문장 → priority', () => {
  assert.equal(priorityFromText('저소음 우선'), 'quiet')
  assert.equal(priorityFromText('성능 우선'), 'performance')
  assert.equal(priorityFromText('균형형'), 'value')
  assert.equal(priorityFromText('소음은 지금보다 커지지 않았으면 좋겠습니다'), 'quiet')
  assert.equal(priorityFromText(''), 'value')
})

test('성능 목표 → resolution (QHD 144Hz·60Hz 는 화소 처리량이 가까운 단계로 근사)', () => {
  assert.equal(resolutionFromText('QHD 144Hz'), 'QHD_165')
  assert.equal(resolutionFromText('QHD 60Hz'), 'FHD_144')
  assert.equal(resolutionFromText('4K 60Hz'), '4K')
  assert.equal(resolutionFromText('FHD 240Hz'), 'FHD_144')
  assert.equal(resolutionFromText('추천값'), null)
  assert.equal(resolutionFromText(''), null)
})

test('점검 질문에서 바꿀 부품 찾기 — 없으면 GPU', () => {
  assert.deepEqual(upgradePartsFromText('GPU만 바꾸면 되는지, RAM도 추가해야 하는지'), ['GPU', 'RAM'])
  assert.deepEqual(upgradePartsFromText('그래픽카드를 바꾸고 싶어요'), ['GPU'])
  assert.deepEqual(upgradePartsFromText('SSD 용량이 부족해요'), ['저장장치'])
  assert.deepEqual(upgradePartsFromText('뭘 바꾸면 좋을까요'), ['GPU'])
})

test('점검 행 → current_specs: 백엔드가 아는 부품만, 사용자가 쓴 원문으로', () => {
  const row = (part, original) => ({ part, original, originalNote: '', matched: 'x', matchedNote: '', state: 'ok', stateLabel: '확인' })
  const specs = currentSpecsFromRows([
    row('CPU', 'AMD Ryzen 7 7800X3D'), row('GPU', 'RTX 4070 SUPER'), row('BOARD', 'B650M WiFi'),
    row('SSD', 'Samsung 990 PRO 1TB'), row('DISPLAY', 'LG 27GR95QE'), row('RAM', '   '),
    row('파워', '750W'),                                       // 이미 백엔드 슬롯 이름 그대로인 행(업로드 결과)도 그대로 통과한다
  ])
  assert.deepEqual(specs, {
    CPU: 'AMD Ryzen 7 7800X3D', GPU: 'RTX 4070 SUPER', 메인보드: 'B650M WiFi', 저장장치: 'Samsung 990 PRO 1TB', 파워: '750W',
  })   // DISPLAY(모니터)는 여전히 뺀다 — computer의 slot_structure에 대응 슬롯이 없다
})

test('backendSlotFromRowPart: 화면 전용 라벨(BOARD·SSD)은 백엔드 슬롯으로, 대응 없으면(모니터) null', () => {
  assert.equal(backendSlotFromRowPart('BOARD'), '메인보드')
  assert.equal(backendSlotFromRowPart('SSD'), '저장장치')
  assert.equal(backendSlotFromRowPart('CPU'), 'CPU')
  assert.equal(backendSlotFromRowPart('케이스'), '케이스')
  assert.equal(backendSlotFromRowPart('DISPLAY'), null)
})

test('추천 항목 → 화면 부품: 가격·이유·리뷰 없음 처리', () => {
  const item = itemFromWire(wireItem())
  assert.equal(item.key, 'cpu')
  assert.equal(item.price, 221750)
  assert.equal(item.fit, '게임 성능 대비 가격이 좋습니다.')
  assert.equal(item.reviews, '없음')           // 관측이 없으면 0건이 아니라 "없음"
  assert.equal(item.rating, '-')
  assert.equal(item.score, '예산의 17%')
  assert.equal(item.source, '데모 가격')
  assert.deepEqual(item.tags, ['데모 가격', '대안 3개'])
  assert.equal(item.label, 'CPU')
})

test('수량은 가격에 곱하고, 리뷰·평점·수집 가격이 있으면 그대로 보인다', () => {
  const item = itemFromWire(wireItem({
    slot: '쿨러', slot_label: '쿨러', qty: 2, price: 38200, price_source: 'observed', price_observed_at: '2026-09-18T00:00:00Z',
    review: { total_count: 1234, rating_refined: 4.36 }, timing: 'later', alternatives_count: 0,
  }))
  assert.equal(item.key, 'cooler')
  assert.equal(item.price, 76400)
  assert.equal(item.reviews, '1,234개')
  assert.equal(item.rating, '4.4')
  assert.equal(item.source, '수집 가격 · 2026-09-18')
  assert.equal(item.actionClass, 'later')
  assert.deepEqual(item.tags, ['수집 가격'])
})

test('이유 문장이 아직 없으면 정리 중, 실패하면 실패로 알린다', () => {
  assert.match(itemFromWire(wireItem({ reason: { status: 'pending', text: null } })).fit, /정리하고 있습니다/)
  assert.match(itemFromWire(wireItem({ reason: { status: 'failed', text: null } })).fit, /만들지 못했습니다/)
})

test('추천 결과 → 구성: 선택한 항목만, 점검 초안은 복사', () => {
  const draft = { question: 'q', budget: '1,000,000원', rows: [] }
  const plan = planFromResult(
    { list_id: 'L1', status: 'done', explanation: { status: 'ready' }, error: null,
      items: [wireItem(), wireItem({ item_id: 'it-2', slot: 'GPU', selected: false })] },
    { mode: 'upgrade', budget: 800000, conditions: { intent: 'i', performance: '', quiet: '' }, checkSnapshot: draft },
  )
  assert.equal(plan.id, 'L1')
  assert.equal(plan.mode, 'upgrade')
  assert.equal(plan.items.length, 1)
  assert.deepEqual(plan.checkSnapshot, draft)
  assert.notEqual(plan.checkSnapshot, draft)
})

test('확정 리포트 → 저장한 구성: 서버 값 + 화면 전용 값, 없으면 기본값', () => {
  const report = {
    list_id: 'L1', name: '내 PC', planned_purchase_at: '2026-10-30T00:00:00+09:00', target_amount: null, memo: '메모',
    total: 100000, confirmed_at: '2026-09-21T10:00:00+09:00',
    items: [{ slot: '메인보드', slot_label: '메인보드', product: { name: 'GIGABYTE A620M H' }, price: 88500, qty: 1, timing: 'now',
      review: null, evidence_text: '소켓이 맞습니다.' }],
  }
  const setup = setupFromReport(report, undefined)
  assert.equal(setup.id, 'L1')
  assert.equal(setup.date, '2026-10-30')
  assert.equal(setup.target, 100000)            // 목표 금액이 없으면 합계
  assert.equal(setup.plan.items[0].key, 'board')
  assert.equal(setup.plan.items[0].fit, '소켓이 맞습니다.')
  assert.equal(setup.desk.deskUnlocked, false)
  assert.deepEqual(setup.checkDraft.rows, [])
  assert.equal(setup.plan.mode, 'new')
})

test('문장에서 게임 제목을 찾는다 — 백엔드 요구사양 표가 아는 이름으로', () => {
  assert.deepEqual(gamesFromText('QHD로 사이버펑크 2077이랑 엘든링 할 거예요'), ['엘든링', '사이버펑크 2077'])
  assert.deepEqual(gamesFromText('롤이랑 발로란트, 배그'), ['발로란트', '리그 오브 레전드', '배틀그라운드'])
  assert.deepEqual(gamesFromText('Black Myth Wukong and LoL'), ['리그 오브 레전드', '검은 신화: 오공'])
  assert.deepEqual(gamesFromText('롤러코스터 타이쿤'), [])   // 한글 안의 "롤" 은 게임이 아니다
  assert.deepEqual(gamesFromText('사무용 PC 추천'), [])
})

test('구매 전 확인: 준비된 문장만 " · " 로 나눠 보이고, 준비 전·실패는 빈 목록', () => {
  const ready = { status: 'ready', text: '사용 가이드: 소켓을 확인하세요. · 파워 용량: 스펙이 부족해 확인하지 못했습니다.' }
  assert.deepEqual(checksFromWire(ready), ['사용 가이드: 소켓을 확인하세요.', '파워 용량: 스펙이 부족해 확인하지 못했습니다.'])
  assert.deepEqual(checksFromWire({ status: 'pending', text: null }), [])
  assert.deepEqual(checksFromWire({ status: 'failed', text: null }), [])
  assert.deepEqual(checksFromWire(undefined), [])
  assert.deepEqual(itemFromWire(wireItem({ checks: ready })).checks, checksFromWire(ready))
})

test('세트 검증 → 호환 안내: major 는 문제, minor 는 확인 못 한 항목', () => {
  const verification = {
    status: 'ready',
    issues: [
      { axis: 'socket', severity: 'major', text: 'CPU·메인보드 소켓: 관측값 fail' },
      { axis: 'bios', severity: 'minor', text: '메인보드 BIOS 지원 버전: 확인하지 못했습니다.' },
    ],
  }
  assert.deepEqual(compatFromWire(verification), {
    problems: ['CPU·메인보드 소켓: 관측값 fail'], unchecked: ['메인보드 BIOS 지원 버전: 확인하지 못했습니다.'],
  })
  assert.equal(compatFromWire({ status: 'pending', issues: [] }), undefined)
  assert.equal(compatFromWire(undefined), undefined)
})

test('부품 근거를 묻는 말은 화면에서 답하고, 바꾸라는 말이 섞이면 서버로 보낸다', () => {
  assert.equal(wantsPartExplanation('이 부품 왜 추천했어?'), true)
  assert.equal(wantsPartExplanation('근거 자세히 알려줘'), true)
  assert.equal(wantsPartExplanation('그래픽카드 이유는 알겠고 더 저렴한 걸로 바꿔줘'), false)
  assert.equal(wantsPartExplanation('케이스 흰색으로 교체해줘'), false)
  assert.equal(wantsPartExplanation('안녕하세요'), false)
  const part = itemFromWire(wireItem())
  assert.equal(partExplanation(part), ['AMD Ryzen 5 7600', 'CPU 추천 이유', '게임 성능 대비 가격이 좋습니다.', '가격: 221,750원'].join('\n'))
})

test('서버 업그레이드 추천 → 점검 화면 제안 카드: 서버가 계산하지 않은 값은 비운다', () => {
  const draft = { question: 'GPU만 바꾸면 될까요?', budget: '1,000,000원', rows: [{ part: 'GPU', original: 'RTX 4070 SUPER', originalNote: '', matched: 'x', matchedNote: '', state: 'ok', stateLabel: '' }] }
  const item = itemFromWire(wireItem({ slot: 'GPU', slot_label: 'GPU', price: 498990, qty: 1, product: { name: 'NVIDIA GeForce RTX 3060 (12GB)', brand: 'NVIDIA', spec_summary: '성능 티어 7' } }))
  const plan = { id: 'p', mode: 'upgrade', items: [item], budget: 1000000, conditions: { intent: '', performance: '', quiet: '' }, checkSnapshot: draft,
    compat: { problems: [], unchecked: ['a', 'b'] } }
  const s = suggestionFromPlan(plan, draft)
  assert.equal(s.part, 'GPU')
  assert.equal(s.productName, 'NVIDIA GeForce RTX 3060 (12GB)')
  assert.equal(s.extraCost, 498990)
  assert.equal(s.currentNote, '현재 입력: RTX 4070 SUPER')
  assert.equal(s.performance, '')
  assert.equal(s.power, '')
  assert.match(s.checkConditions, /확인하지 못한 항목 2개/)
  assert.match(suggestionFromPlan({ ...plan, compat: { problems: ['CPU·메인보드 소켓: 관측값 fail'], unchecked: [] } }, draft).checkConditions, /소켓/)
  assert.equal(suggestionFromPlan({ ...plan, items: [] }, draft), null)
})

test('호환 검사 상세: 화면 목록으로 바꾸고, 모르는 상태는 unknown 으로 본다', () => {
  const wire = [
    { axis: 'socket', label: 'CPU·메인보드 소켓', state: 'ok', detail: 'CPU (AM5) = 메인보드 (AM5)' },
    { axis: 'bios', label: 'BIOS', state: 'skipped', detail: '건너뜀' },
    { axis: 'x', label: '새 검사', state: '처음 보는 상태', detail: '?' },
  ]
  const checks = compatChecksFromWire(wire)
  assert.deepEqual(checks.map(c => c.state), ['ok', 'skipped', 'unknown'])
  assert.equal(checks[0].detail, 'CPU (AM5) = 메인보드 (AM5)')
  assert.equal(compatChecksFromWire([]), undefined)
  assert.equal(compatChecksFromWire(undefined), undefined)
})

test('조건 세션 필드: 값을 그대로 옮기고, 모르는 status는 missing으로 본다', () => {
  const fields = fieldsFromWire([
    { key: 'budget_max', label: '예산', value: 1500000, display: null, status: 'confirmed', editable: true },
    { key: 'priority', label: '우선순위', value: 'quiet', display: '저소음', status: 'assumed', editable: true },
    { key: 'games', label: '게임', value: [], display: null, status: '알 수 없는 상태', editable: true },
  ])
  assert.deepEqual(fields[0], { key: 'budget_max', label: '예산', value: 1500000, display: null, status: 'confirmed' })
  assert.equal(fields[1].display, '저소음')
  assert.equal(fields[2].status, 'missing')
})

test('다음 질문 선택지: 객관식이면 칩으로, 자유 텍스트·선택지 없음·질문 없음이면 undefined', () => {
  const single = { id: 'q_priority', field: 'priority', text: '가장 중요한 건?', select: 'single',
    options: [{ value: 'performance', label: '성능 우선' }, { value: 'value', label: '가성비' }] }
  // 화면에는 label 만 보이고, value 는 서버 내부 값이라 어느 질문의 답인지(questionId)와 함께 /answer 로 보낸다.
  assert.deepEqual(choicesFromWire(single), [
    { label: '성능 우선', value: 'performance', questionId: 'q_priority' },
    { label: '가성비', value: 'value', questionId: 'q_priority' },
  ])
  assert.equal(choicesFromWire({ ...single, select: 'free', options: [] }), undefined)
  assert.equal(choicesFromWire({ ...single, options: [] }), undefined)
  assert.equal(choicesFromWire(null), undefined)
})

test('조건 세션 응답 문장: 가장 최근 assistant 메시지, 없으면 빈 문자열', () => {
  const state = { list_id: 'l1', category: 'computer', mode: 'build', can_recommend: false, next_question: null, fields: [],
    messages: [{ id: '1', role: 'user', text: '150만원짜리 게임용', created_at: 't' },
               { id: '2', role: 'assistant', text: '예산을 1,500,000원으로 기록했어요.', created_at: 't' }] }
  assert.equal(replyTextFromWire(state), '예산을 1,500,000원으로 기록했어요.')
  assert.equal(replyTextFromWire({ ...state, messages: [] }), '')
})

test('사양 매칭 미리보기 응답 → 표 행: 판정만 옮기고 originalNote는 비워 둔다(호출자가 채운다)', () => {
  const rows = reviewRowsFromWire([
    { part: 'GPU', original: 'RTX 4070 SUPER', matched: 'NVIDIA GeForce RTX 4070 SUPER', matched_note: '카탈로그 제품과 일치', state: 'ok' },
    { part: '메인보드', original: '아무 보드', matched: '아무 보드', matched_note: '확인 가능한 스펙이 없습니다.', state: 'warn' },
  ])
  assert.deepEqual(rows, [
    { part: 'GPU', original: 'RTX 4070 SUPER', originalNote: '', matched: 'NVIDIA GeForce RTX 4070 SUPER', matchedNote: '카탈로그 제품과 일치', state: 'ok', stateLabel: '확인' },
    { part: '메인보드', original: '아무 보드', originalNote: '', matched: '아무 보드', matchedNote: '확인 가능한 스펙이 없습니다.', state: 'warn', stateLabel: '확인 필요' },
  ])
})

test('기여도: 큰 축부터 정렬하고, 없거나 비면 undefined — 옛 목업 값(41/33/26)으로 채우지 않는다', () => {
  assert.deepEqual(contributionFromWire({ status: 'ready', contribution: { 가격: 20, 성능: 50, 리뷰: 30 } }), [
    { axis: '성능', percent: 50 }, { axis: '리뷰', percent: 30 }, { axis: '가격', percent: 20 },
  ])
  assert.equal(contributionFromWire({ status: 'ready', contribution: null }), undefined)
  assert.equal(contributionFromWire({ status: 'ready', contribution: {} }), undefined)
  assert.equal(contributionFromWire({ status: 'pending' }), undefined)
  assert.equal(contributionFromWire(undefined), undefined)
})

test('추천 결과 → 계획에 기여도가 실린다', () => {
  const plan = planFromResult({ list_id: 'l', status: 'done', items: [], explanation: { status: 'ready', contribution: { 가격: 60, 성능: 40 } }, error: null },
    { mode: 'new', budget: null, conditions: { intent: '', performance: '', quiet: '' }, checkSnapshot: null })
  assert.deepEqual(plan.contribution, [{ axis: '가격', percent: 60 }, { axis: '성능', percent: 40 }])
})

test('예산 사전 경고: 보여 줄 문장이 있을 때만 경고다', () => {
  assert.deepEqual(budgetWarningFromWire({ level: 'infeasible', message: '예산이 부족해요', estimated_min: 2, budget: 1 }), { level: 'infeasible', message: '예산이 부족해요' })
  assert.equal(budgetWarningFromWire({ level: 'tight', message: null, estimated_min: 2, budget: 3 }), null)
  assert.equal(budgetWarningFromWire(null), null)
  assert.equal(budgetWarningFromWire(undefined), null)
})

test('용도: "게임"이라는 말 없이 게임 제목만 적어도 game — 아니면 게임 요구사양이 서버로 안 간다', () => {
  assert.equal(purposeFromText('사이버펑크 2077 FHD 돌아가는 pc 200만원'), 'game')
  assert.equal(purposeFromText('엘든링 QHD로 돌리고 싶어'), 'game')
  assert.deepEqual(gamesFromText('사이버펑크 2077 FHD 돌아가는 pc 200만원'), ['사이버펑크 2077'])
  // 게임 제목이 없으면 종전대로
  assert.equal(purposeFromText('영상 편집용'), 'creation')
  assert.equal(purposeFromText('그냥 컴퓨터 하나 필요해'), 'other')
  // 제목이 있어도 다른 용도 단어가 먼저 게임으로 잡히는 종전 규칙은 그대로다("게임"이 있으면 game)
  assert.equal(purposeFromText('게임도 하고 영상 편집도 해요'), 'game')
})

test('예산 남김 안내: 문장이 있을 때만 화면용으로 옮기고, 추천 결과에도 실린다', () => {
  const wire = { message: '예산 2,000,000원 중 1,516,788원을 썼어요.', budget: 2000000, spent: 1516788, remaining: 483212, suggest_priority: 'performance' }
  assert.deepEqual(budgetNoticeFromWire(wire), { message: wire.message, remaining: 483212 })
  assert.equal(budgetNoticeFromWire(null), undefined)
  assert.equal(budgetNoticeFromWire(undefined), undefined)
  const plan = planFromResult({ list_id: 'l', status: 'done', items: [], explanation: { status: 'ready' }, budget_notice: wire, error: null },
    { mode: 'new', budget: 2000000, conditions: { intent: '', performance: '', quiet: '' }, checkSnapshot: null })
  assert.deepEqual(plan.budgetNotice, { message: wire.message, remaining: 483212 })
})

test('조립·설치 가이드: 서버 문장을 단계(제목 + 설치/확인 줄)로 나눈다', () => {
  const text = '1. 케이스 — 다크플래쉬 DLM21\n   설치: 옆판을 열고 눕히세요.\n   확인: 팬 장착 개수를 보세요.\n2. GPU — RTX 4070 SUPER\n   설치: PCIe 슬롯에 끝까지 꽂으세요.\n   확인: 파워 용량을 확인하세요.'
  assert.deepEqual(guideStepsFromWire({ status: 'ready', text }), [
    { title: '케이스 — 다크플래쉬 DLM21', lines: [{ label: '설치', text: '옆판을 열고 눕히세요.' }, { label: '확인', text: '팬 장착 개수를 보세요.' }] },
    { title: 'GPU — RTX 4070 SUPER', lines: [{ label: '설치', text: 'PCIe 슬롯에 끝까지 꽂으세요.' }, { label: '확인', text: '파워 용량을 확인하세요.' }] },
  ])
})

test('조립·설치 가이드: 이 조합·주의 줄도 라벨로 나누고, 0번 준비 단계도 단계로 읽는다', () => {
  const text = '0. 준비\n   설치: 정전기를 방전하세요.\n1. 그래픽카드 장착\n   이 조합: GPU 길이 — GPU 길이 305mm ≤ 케이스 허용 400mm\n   주의: 케이블을 정리하세요.\n   확인: 드라이버를 설치하세요.'
  assert.deepEqual(guideStepsFromWire({ status: 'ready', text }), [
    { title: '준비', lines: [{ label: '설치', text: '정전기를 방전하세요.' }] },
    { title: '그래픽카드 장착', lines: [
      { label: '이 조합', text: 'GPU 길이 — GPU 길이 305mm ≤ 케이스 허용 400mm' },
      { label: '주의', text: '케이블을 정리하세요.' },
      { label: '확인', text: '드라이버를 설치하세요.' },
    ] },
  ])
})

test('조립·설치 가이드: 에이전트식 마크다운·글머리·모르는 줄도 버리지 않는다', () => {
  const text = '1) **CPU — Ryzen 7**\n- 설치: 삼각형 표시를 맞추세요\n※ 핀을 만지지 마세요'
  assert.deepEqual(guideStepsFromWire({ status: 'ready', text }), [
    { title: 'CPU — Ryzen 7', lines: [{ label: '설치', text: '삼각형 표시를 맞추세요' }, { label: '', text: '※ 핀을 만지지 마세요' }] },
  ])
})

test('조립·설치 가이드: 준비 전·실패·빈 문장·없음이면 undefined — 화면이 일반 안내로 대신한다', () => {
  assert.equal(guideStepsFromWire({ status: 'pending', text: null }), undefined)
  assert.equal(guideStepsFromWire({ status: 'failed', text: null }), undefined)
  assert.equal(guideStepsFromWire({ status: 'ready', text: '  \n ' }), undefined)
  assert.equal(guideStepsFromWire(null), undefined)
  assert.equal(guideStepsFromWire(undefined), undefined)
})

test('리포트 → 저장한 구성에 가이드가 실리고, 없으면 careGuide 는 undefined', () => {
  const report = { list_id: 'l', name: 'n', planned_purchase_at: null, target_amount: null, memo: '', total: 1, confirmed_at: '2026-09-26T00:00:00Z', items: [] }
  const withGuide = setupFromReport({ ...report, care_guide: { status: 'ready', text: '1. GPU — X\n   설치: 꽂으세요.' } }, undefined)
  assert.deepEqual(withGuide.careGuide, [{ title: 'GPU — X', lines: [{ label: '설치', text: '꽂으세요.' }] }])
  assert.equal(setupFromReport(report, undefined).careGuide, undefined)
})
