import * as echarts from 'echarts';

import type { AgentInsight, ChartSpec, QueryResult } from './types';

const CHART_ELEMENT_ID = 'analysis-result-chart';

export function exportResultCsv(result: QueryResult, title: string): void {
  const lines = [
    result.columns.map(csvCell).join(','),
    ...result.rows.map((row) => result.columns.map((column) => csvCell(row[column])).join(',')),
  ];
  downloadBlob(
    `\uFEFF${lines.join('\r\n')}`,
    `${safeFilename(title)}${result.truncated ? '-partial' : ''}.csv`,
    'text/csv;charset=utf-8',
  );
}

export function exportChartPng(title: string): void {
  const dataUrl = chartDataUrl();
  if (!dataUrl) throw new Error('当前没有可导出的图表');
  const anchor = document.createElement('a');
  anchor.href = dataUrl;
  anchor.download = `${safeFilename(title)}.png`;
  anchor.click();
}

export function exportHtmlReport(input: {
  title: string;
  question: string;
  result: QueryResult;
  insight: AgentInsight | null;
  chartSpec: ChartSpec | null;
  querySpec: Record<string, unknown> | null;
  warnings: Array<{ code: string; message: string }>;
}): void {
  const { title, question, result, insight, chartSpec, querySpec, warnings } = input;
  const chart = chartDataUrl();
  const tableHead = result.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('');
  const tableRows = result.rows
    .map(
      (row) =>
        `<tr>${result.columns
          .map((column) => `<td>${escapeHtml(formatExportValue(row[column]))}</td>`)
          .join('')}</tr>`,
    )
    .join('');
  const observations = insight?.observations.length
    ? `<ul>${insight.observations.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
    : '<p>无补充观察。</p>';
  const limitations = insight?.limitations.length
    ? `<ul>${insight.limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
    : '<p>无额外限制说明。</p>';
  const warningList = warnings.length
    ? `<h2>执行告警</h2><ul>${warnings
        .map((warning) => `<li><strong>${escapeHtml(warning.code)}</strong>：${escapeHtml(warning.message)}</li>`)
        .join('')}</ul>`
    : '';
  const queryScope = querySpec
    ? `<h2>查询口径</h2><pre>${escapeHtml(JSON.stringify(querySpec, null, 2))}</pre>`
    : '<h2>查询口径</h2><p>本次结果未返回可复用的 QuerySpec。</p>';
  const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#182230;margin:40px;line-height:1.55}
h1{font-size:24px;margin-bottom:4px}h2{font-size:17px;margin-top:28px}.meta{color:#667085;font-size:13px}
.notice{background:#f2f4f7;border-radius:8px;padding:12px 16px}.chart{max-width:100%;margin:12px 0 20px}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #d0d5dd;padding:7px;text-align:left}th{background:#f2f4f7}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;border:1px solid #eaecf0;border-radius:8px;padding:12px;font-size:12px}
@media print{body{margin:18mm}.no-print{display:none}}
</style></head><body>
<h1>${escapeHtml(title)}</h1>
<div class="meta">生成时间：${escapeHtml(new Date().toLocaleString())} · ${result.row_count} 行 · ${result.query_time_ms} ms</div>
${result.truncated ? '<p class="notice">当前查询结果已截断，本报告仅包含本次返回的数据。</p>' : ''}
<h2>分析问题</h2><p>${escapeHtml(question || '结构化查询')}</p>
${queryScope}
<h2>结论摘要</h2><div class="notice">${escapeHtml(insight?.summary ?? '本报告展示当前受控聚合查询结果。')}</div>
${warningList}
${chart ? `<h2>${escapeHtml(chartSpec?.title ?? '结果图表')}</h2><img class="chart" src="${chart}" alt="结果图表">` : ''}
<h2>主要观察</h2>${observations}
<h2>结果数据</h2><table><thead><tr>${tableHead}</tr></thead><tbody>${tableRows}</tbody></table>
<h2>限制与声明</h2>${limitations}<p class="meta">本结果仅用于统计分析与实训展示，不构成诊断、治疗建议或临床决策。</p>
</body></html>`;
  downloadBlob(html, `${safeFilename(title)}.html`, 'text/html;charset=utf-8');
}

function chartDataUrl(): string | null {
  const element = document.getElementById(CHART_ELEMENT_ID);
  if (!element) return null;
  if (element.dataset.chartable !== 'true') return null;
  const instance = echarts.getInstanceByDom(element);
  if (!instance) return null;
  return instance.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
}

function csvCell(value: unknown): string {
  let text = formatExportValue(value);
  if (/^[\t\r\n ]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

function formatExportValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function safeFilename(value: string): string {
  const normalized = value.trim().replace(/[\\/:*?"<>|\u0000-\u001F]/g, '_').slice(0, 80);
  return normalized || 'medical-ai-analysis';
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function downloadBlob(content: string, filename: string, type: string): void {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export { CHART_ELEMENT_ID };
