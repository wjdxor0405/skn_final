import { useParams, useNavigate } from 'react-router-dom'
import type { GuideLine, SavedSetup } from '../state/types'
import { useSetups } from '../state/SetupsContext'
import { usePlan } from '../state/PlanContext'
import { planTotal, reportText } from '../state/planModel'
import { MissingPage } from './MissingPage'
import { PageLoading } from '../components/layout/PageLoading'
import { wonFmt } from '../utils/format'
import { useToast } from '../state/ToastContext'
import { assemblyGuide } from '../data/assemblyGuide'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { isMockApi } from '../api'
import { QUIET_LABEL } from '../state/conditionLabels'

export function ReportPage() {
  const { setupId } = useParams()
  const { savedSetups, loading, storageError } = useSetups()
  const report = savedSetups.find(setup => setup.id === setupId)
  if (loading) return <PageLoading />
  if (!report) return <MissingPage title="저장된 리포트를 찾을 수 없습니다." description={storageError || (isMockApi ? '이 브라우저에 저장되지 않았거나 삭제된 구성입니다. 플래너의 내 구성 메뉴에서 저장 목록을 확인해주세요.' : '로그인하지 않았거나 삭제된 구성입니다. 로그인한 뒤 플래너의 내 구성 메뉴에서 저장 목록을 확인해주세요.')} />
  return <ReportView key={report.id} report={report} />
}

const GUIDE_LABEL_CLASS: Record<GuideLine['label'], string> = {
  '설치': 'install', '이 조합': 'combo', '주의': 'caution', '확인': 'after-check', '': '',
}

function ReportView({ report }: { report: SavedSetup }) {
  const navigate = useNavigate()
  const { loadFromSavedSetup } = usePlan()
  const { showToast } = useToast()
  useDocumentTitle(report.title)
  const total = planTotal(report.plan)
  const confirmedAt = new Date(report.savedAt).toLocaleString('ko-KR')

  function handlePrint() {
    window.print()
  }

  function handleDownload() {
    const text = reportText(report)
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = (report.title || 'truefit-list') + '.txt'
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    showToast('리스트 파일을 저장했습니다.')
  }

  return (
    <section className="landing" aria-labelledby="reportTitle">
      <div className="landing-inner report-inner">
        <div className="report-banner">
          <p className="eyebrow">PLAN SAVED · MY REPORT</p>
          <h1 id="reportTitle">{report.title}</h1>
          <p className="setup-hint">{isMockApi ? '이 브라우저에 임시 저장 · ' : ''}구매 예정: {report.date}</p>
          <div className="report-banner-sums">
            <div><span>예상 총액</span><strong>{wonFmt(total)}</strong></div>
            <div><span>목표 가격</span><strong>{wonFmt(report.target)}</strong></div>
          </div>
        </div>
        <div className="report-actions">
          <button type="button" className="ghost-btn" onClick={handlePrint}>리포트 인쇄 / PDF</button>
          <button type="button" className="ghost-btn" onClick={handleDownload}>리스트 파일 저장</button>
          <button type="button" className="ghost-btn" onClick={() => { loadFromSavedSetup(report); navigate('/plan') }}>추천 다시 보기</button>
        </div>
        <div className="report-panel">
          <h2>{report.plan.mode === 'upgrade' ? '업그레이드 구매 리스트' : '구매 리스트'}</h2>
          <p style={{ whiteSpace: 'pre-line' }}>사용 목적: {report.plan.conditions.intent || '미입력'}<br />성능: {report.plan.conditions.performance || '미입력'} · {QUIET_LABEL}: {report.plan.conditions.quiet || '미입력'}<br />예산: {report.plan.budget === null ? '미설정' : wonFmt(report.plan.budget)}</p>
          {report.plan.checkSnapshot && <ul>{report.plan.checkSnapshot.rows.map(row => <li key={row.part}>기존 {row.part}: {row.matched}</li>)}</ul>}
          <p>책상: {report.desk.deskWidth} × {report.desk.deskDepth} × {report.desk.deskHeight}mm</p>
          <div className="report-table-wrap" tabIndex={0} role="region" aria-label="구매 리스트 표, 작은 화면에서는 가로로 스크롤하세요">
            <table className="report-table">
              <thead><tr><th>제품 사진</th><th>제품</th><th>가격</th><th>리뷰</th><th>추천 근거</th></tr></thead>
              <tbody>
                {report.plan.items.map(p => (
                  <tr key={p.id}>
                    <td><span className="report-thumb">{p.label}</span></td>
                    <td><strong>{p.type}</strong><br />{p.name}<br /><button type="button" className="report-product-link" onClick={() => showToast(isMockApi ? '실제 상품 페이지로 연결되지 않는 목업입니다.' : '판매처 링크는 아직 연결되지 않았습니다.')}>상품 페이지 ↗</button></td>
                    <td>{wonFmt(p.price)}</td>
                    <td><span className="report-review-badge">리뷰 {p.reviews.replace('개', '건')}</span></td>
                    <td>{p.fit}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr><th colSpan={2}>전체 가격</th><td colSpan={3}>{wonFmt(total)}</td></tr></tfoot>
            </table>
          </div>
          <p>{report.memo && report.memo.trim() ? report.memo.trim() : '추가 메모가 없습니다.'}</p>
        </div>
        <div className="report-panel">
          <h2>조립·설치 가이드</h2>
          {report.careGuide?.length ? <>
            <ol className="guide-steps">
              {report.careGuide.map((step, i) => <li key={i}>
                {step.title && <strong>{step.title}</strong>}
                {step.lines.map((line, j) => <p key={j}>{line.label && <span className={'guide-label ' + GUIDE_LABEL_CLASS[line.label]}>{line.label}</span>}{line.text}</p>)}
              </li>)}
            </ol>
            <p className="setup-hint">일반 조립 상식으로 작성한 안내에 이 견적의 호환 검사 결과를 붙였습니다. 부품별 제품 설명서를 함께 확인해주세요.</p>
          </> : <>
            {assemblyGuide(report.plan).map((step, i) => <p key={i}>{i + 1}. {step}</p>)}
            <p>{isMockApi ? '(목업 예시 문장입니다)' : '(일반 조립 안내입니다. 부품별 설명서를 함께 확인해주세요.)'}</p>
          </>}
        </div>
        <p className="setup-hint">확정 시점 {confirmedAt}</p>
      </div>
    </section>
  )
}
