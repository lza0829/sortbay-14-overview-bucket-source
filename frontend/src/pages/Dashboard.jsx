import React, { useEffect, useState } from 'react'
import { api } from '../api'

const 格序 = ['待路由', '已路由', '错分回流', '已封袋']
const 格提示 = {
  待路由: '已装载、尚未裁决',
  已路由: '已裁决落入、未封袋（口门在口）',
  错分回流: '已摘出口门、待再路由',
  已封袋: '袋已封口',
}

export default function Dashboard() {
  const [data, setData] = useState(null)
  const [rec, setRec] = useState(null)
  const [trace, setTrace] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = async () => {
    setErr('')
    try {
      const d = await api('/调度屏')
      setData(d)
      setRec(await api('/调度屏/对账'))
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])

  const showTrace = async () => {
    setErr('')
    try { setTrace(await api('/调度屏/轨迹')) } catch (e) { setErr(e.message) }
  }

  const manualAdjust = async () => {
    setErr(''); setMsg('')
    try {
      await api('/调度屏/手工改计', { method: 'POST', body: '{}' })
      setMsg('（不应发生）改计被接受')
    } catch (e) {
      setErr(`改计被拒：${e.message}`)
    }
  }

  if (err && !data) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>

  const cells = data.四格 || {}
  const diffs = rec?.差异 || []
  const auditBreaks = rec?.审计缺口 || []
  const aligned = rec?.对齐

  return (
    <div className="panel">
      <h2>调度屏四格</h2>
      <p className="muted">
        当前班次：{data.班次?.business_day} {data.班次?.name} · {data.班次?.sealed ? '已封班' : '未封班'} ·
        真源口径：<b>{data.口径}</b>（装载/裁决/回流/开袋/入袋/封袋事件流重放）
      </p>

      <div className="grid">
        {格序.map((k) => (
          <div className="stat" key={k}>
            <div className="n">{cells[k] ?? 0}</div>
            <div className="l">{k}</div>
            <div className="muted">{格提示[k]}</div>
          </div>
        ))}
      </div>

      <div className="row">
        <button type="button" className="secondary" onClick={load}>刷新</button>
        <button type="button" className="secondary" onClick={showTrace}>重放轨迹</button>
        <button type="button" className="secondary" onClick={manualAdjust}>手工改计（应被拒）</button>
        <span className="muted">对账：{aligned ? '✅ 四格与扫描册/口门/袋册/审计对齐' : '⚠️ 存在差异，见下'}</span>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}

      {rec && !aligned && (
        <>
          <h3>对账差异（事件重放 vs 各旁证）</h3>
          <table>
            <thead><tr><th>来源</th><th>目标</th><th>问题</th><th>明细</th></tr></thead>
            <tbody>
              {diffs.map((d, i) => (
                <tr key={i}><td>{d.来源}</td><td>{d.扫描编号 ?? d.袋编号 ?? '-'}</td><td>{d.问题}</td>
                  <td className="muted">{JSON.stringify(Object.fromEntries(Object.entries(d).filter(([k]) => !['来源', '扫描编号', '袋编号', '问题'].includes(k))))}</td>
                </tr>
              ))}
              {auditBreaks.map((a, i) => (
                <tr key={`a${i}`}><td>审计</td><td>{a.事件编号}</td><td>审计缺口</td><td>{a.类型}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h3>口门在口（同源旁证）</h3>
      <table>
        <thead><tr><th>口门</th><th>标题</th><th>状态</th><th>在口件数</th></tr></thead>
        <tbody>
          {(data.口门在口 || []).map((c) => (
            <tr key={c.口门编号}><td>{c.口门码}</td><td>{c.标题}</td><td>{c.状态}</td><td>{c.在口件数}</td></tr>
          ))}
        </tbody>
      </table>

      <h3>袋册封态（同源）</h3>
      <table>
        <thead><tr><th>袋编号</th><th>口门编号</th><th>状态</th><th>件数</th></tr></thead>
        <tbody>
          {(data.袋册 || []).length === 0 && <tr><td colSpan={4} className="muted">暂无袋</td></tr>}
          {(data.袋册 || []).map((b) => (
            <tr key={b.袋编号}><td>{b.袋编号}</td><td>{b.口门编号}</td><td>{b.状态}</td><td>{b.件数}</td></tr>
          ))}
        </tbody>
      </table>

      {trace && (
        <>
          <h3>事件流重放轨迹（{trace.轨迹.length} 条）</h3>
          <table>
            <thead><tr><th>事件</th><th>类型</th><th>扫描</th><th>袋</th><th>批次</th><th>口门</th><th>落格</th><th>时间</th></tr></thead>
            <tbody>
              {trace.轨迹.map((t, i) => (
                <tr key={`${t.事件编号}-${t.扫描编号}-${i}`}>
                  <td>{t.事件编号}</td><td>{t.类型}</td><td>{t.扫描编号 ?? '-'}</td>
                  <td>{t.袋编号 ?? '-'}</td><td>{t.批次编号 ?? '-'}</td><td>{t.口门编号 ?? '-'}</td>
                  <td>{t.落格 || '—'}</td><td className="muted">{t.时间}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
