import { describe, expect, it, vi } from 'vitest';

import { exportHtmlReport, exportResultCsv } from '../exports';
import type { QueryResult } from '../types';

const result: QueryResult = {
  columns: ['label', 'value'],
  rows: [{ label: '\t=HYPERLINK("bad")', value: 12 }],
  row_count: 1,
  query_time_ms: 8,
  truncated: false,
  metadata: {},
};

describe('result exports', () => {
  it('writes UTF-8 CSV and neutralizes spreadsheet formulas', async () => {
    const blobs: Blob[] = [];
    installObjectUrlMocks(blobs);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    exportResultCsv(result, '费用/分析');

    expect(blobs).toHaveLength(1);
    const content = await readBlob(blobs[0]);
    expect(content).toContain('"label","value"');
    expect(content).toContain('"\'\t=HYPERLINK(""bad"")"');
  });

  it('escapes report text and never emits executable result markup', async () => {
    const blobs: Blob[] = [];
    installObjectUrlMocks(blobs);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    exportHtmlReport({
      title: '<script>alert(1)</script>',
      question: '<img src=x onerror=alert(1)>',
      result,
      insight: null,
      chartSpec: null,
      querySpec: {
        table: 'inpatient',
        filters: [{ field: 'DischargeYear', op: '=', value: '<2021>' }],
      },
      warnings: [{ code: 'INSIGHT_UNAVAILABLE', message: '<模型解读不可用>' }],
    });

    const content = await readBlob(blobs[0]);
    expect(content).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(content).toContain('&lt;img src=x onerror=alert(1)&gt;');
    expect(content).not.toContain('<script>alert(1)</script>');
    expect(content).toContain('&lt;2021&gt;');
    expect(content).toContain('INSIGHT_UNAVAILABLE');
    expect(content).toContain('&lt;模型解读不可用&gt;');
    expect(content).toContain('不构成诊断、治疗建议或临床决策');
  });
});

function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener('load', () => resolve(String(reader.result)));
    reader.addEventListener('error', () => reject(reader.error));
    reader.readAsText(blob);
  });
}

function installObjectUrlMocks(blobs: Blob[]): void {
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn((blob: Blob) => {
      blobs.push(blob);
      return 'blob:test';
    }),
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  });
}
