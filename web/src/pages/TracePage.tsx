import { useEffect, useState } from 'react';
import { Card, Table, Tag, message } from 'antd';
import type { TableProps } from 'antd';
import dayjs from 'dayjs';
import { useParams } from 'react-router-dom';
import { fetchTraces } from '@/api/rest';
import type { TraceItem } from '@/types/api';

function TracePage() {
  const { threadId = '' } = useParams<{ threadId: string }>();
  const [rows, setRows] = useState<TraceItem[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    setLoading(true);
    fetchTraces(threadId)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          message.error(error instanceof Error ? error.message : '加载调用链失败');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [threadId]);

  const columns: TableProps<TraceItem>['columns'] = [
    { title: '轮次', dataIndex: 'turn', width: 70 },
    { title: '意图', dataIndex: 'intent', width: 140, render: (v: string | null) => v ?? '-' },
    { title: '路由', dataIndex: 'route', width: 140, render: (v: string | null) => v ?? '-' },
    {
      title: '工具',
      dataIndex: 'tools',
      render: (tools: string[]) =>
        tools.length ? tools.map((t) => <Tag key={t}>{t}</Tag>) : '-',
    },
    {
      title: '耗时',
      dataIndex: 'latency_ms',
      width: 100,
      render: (v: number | null) => (v == null ? '-' : `${v} ms`),
    },
    {
      title: 'Tokens',
      key: 'tokens',
      width: 120,
      render: (_, row) => `${row.tokens_in ?? '-'} / ${row.tokens_out ?? '-'}`,
    },
    {
      title: '降级',
      dataIndex: 'degraded',
      width: 80,
      render: (v: number) => (v ? <Tag color="red">是</Tag> : <Tag>否</Tag>),
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
    },
  ];

  return (
    <Card title={`调用链 · ${threadId || '未指定会话'}`} styles={{ body: { padding: 0 } }}>
      <Table<TraceItem>
        rowKey="trace_id"
        columns={columns}
        dataSource={rows}
        loading={loading}
        pagination={false}
        scroll={{ x: 900 }}
      />
    </Card>
  );
}

export default TracePage;
