import type { CurrentPlan } from '../state/types'

// 목업 예시 문장입니다. 실제 서비스에서는 부품별 조립 가이드 API 응답으로 교체합니다.
export function assemblyGuide(plan: CurrentPlan): string[] {
  if (plan.mode === 'new') return [
    '정전기 방지 손목띠를 차거나 금속을 만져 정전기를 빼고, 케이스를 평평한 곳에 놓아주세요.',
    '메인보드에 CPU와 쿨러를 먼저 장착해요.',
    '케이스에 메인보드를 고정한 뒤 파워서플라이와 그래픽카드를 연결해요.',
  ]
  const parts = plan.items.map(item => item.type).join(', ')
  const isGpu = plan.items.every(item => item.key === 'gpu')
  return [
    'PC 전원을 끄고 전원 케이블을 분리한 뒤, 정전기를 방지하고 케이스 옆면을 열어주세요.',
    isGpu
      ? '기존 그래픽카드의 보조 전원 케이블과 고정 나사, PCIe 슬롯 잠금 장치를 풀어 분리해요.'
      : `교체할 부품(${parts})에 연결된 케이블과 고정 나사를 풀어 기존 부품을 분리해요.`,
    isGpu
      ? '새 그래픽카드를 PCIe 슬롯에 끝까지 꽂고 나사로 고정한 뒤 보조 전원 케이블을 연결해요.'
      : '새 부품을 같은 위치에 장착하고 케이블을 다시 연결해요.',
    isGpu
      ? '케이스를 닫고 부팅한 뒤 기존 드라이버를 제거하고 새 그래픽 드라이버를 설치해요.'
      : '케이스를 닫고 부팅한 뒤 새 부품이 인식되는지 확인해요.',
  ]
}
