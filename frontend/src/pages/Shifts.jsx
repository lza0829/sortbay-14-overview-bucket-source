import React, { useEffect, useState } from 'react'
import { api } from '../api'

const 格序 = ['待路由', '已路由', '错分回流', '已封袋']

export default function Shifts() {
  const [rows, setRows] = useState([])
  const [rec, setRec] = useState(null)
  const [err, setErr] = useState('')
  const load = async () => {
    setRows(await api('/班次'))
    setRec(await api('/班次/对账'))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const seal = async (id) => {
    try { await api(`/班次/${id}/封班`, { method: 'POST' }); load() }
    catch (e) { setErr(e.message) }
  }
  const cells = rec?.四格 || {}
  return (
    <div className="panel">
      <h2>班次对账</h2>
      {err && <div className="err">{err}</div>}
      {rec && (
        <>
          <p className="muted">口径：{rec.口径} · 对齐：{rec.对齐 ? '✅ 一致' : '⚠️ 存在差异'}</p>
          <div className="grid">
            {格序.map((k) => (
              <div className="stat" key={k}><div className="n">{cells[k] ?? 0}</div><div className="l">{k}</div></div>
            ))}
            <div className="stat"><div className="n">{rec.未清件数}</div><div className="l">未清件数</div></div>
          </div>
          <p className="muted">扫描册 {rec.扫描总数} 件 · 重放 {rec.重放件数} 件</p>
          {!rec.对齐 && (
            <table>
              <thead><tr><th>来源</th><th>目标</th><th>问题</th></tr></thead>
              <tbody>
                {(rec.差异 || []).map((d, i) => (
                  <tr key={i}><td>{d.来源}</td><td>{d.扫描编号 ?? d.袋编号 ?? '-'}</td><td>{d.问题}</td></tr>
                ))}
                {(rec.审计缺口 || []).map((a, i) => (
                  <tr key={`a${i}`}><td>审计</td><td>{a.事件编号}</td><td>审计缺口</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
      <table>
        <thead><tr><th>编号</th><th>营业日</th><th>班次</th><th>封班</th><th>操作</th></tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}>
              <td>{s.id}</td><td>{s.business_day}</td><td>{s.name}</td><td>{s.sealed ? '已封班' : '未封班'}</td>
              <td>{!s.sealed && <button type="button" onClick={() => seal(s.id)}>封班</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
