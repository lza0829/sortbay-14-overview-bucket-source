import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Audit() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/审计').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>
  const audit = Array.isArray(data) ? data : data.审计
  return (
    <div className="panel">
      <h2>审计</h2>
      <p className="muted">审计由事件写入统一产出，与装载/裁决/回流/封袋事件流同事务落库。</p>
      <table>
        <thead><tr><th>编号</th><th>分类</th><th>对象</th><th>动作</th><th>时间</th><th>备注</th></tr></thead>
        <tbody>
          {(audit || []).map((a) => (
            <tr key={a.id}><td>{a.id}</td><td>{a.kind}</td><td>{a.ref_id ?? '-'}</td><td>{a.action}</td><td className="muted">{a.created_at}</td><td>{a.note}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
