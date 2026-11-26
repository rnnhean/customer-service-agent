import { useCallback, useEffect, useState } from 'react';
import { Button, Card, Input, Modal, Segmented, Space, Table, Tag, message } from 'antd';
import type { TableProps } from 'antd';
import dayjs from 'dayjs';
import { ApiRequestError, decideApproval, fetchApprovals } from '@/api/rest';
import type { ApprovalStatusFilter } from '@/api/rest';
import type { ApprovalItem } from '@/types/api';

const STATUS_META: Record<ApprovalItem['status'], { color: string; text: string }> = {
  pending: { color: 'gold', text: '待审批' },
  approved: { color: 'green', text: '已同意' },
  rejected: { color: 'red', text: '已驳回' },
  expired: { color: 'default', text: '已过期' },
};

/** 后端实际还会返回 coupon_grant / compensation 等类型，前端兜底显示原值 */
const TYPE_TEXT: Record<string, string> = {
  refund: '退款',
  reissue: '补发',
  coupon_grant: '发券',
  compensation: '补偿',
};

const REVIEWER = 'approval_desk';

interface PendingDecision {
  record: ApprovalItem;
  approved: boolean;
}

function ApprovalPage() {
  const [items, setItems] = useState<ApprovalItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ApprovalStatusFilter>('pending');
  const [deciding, setDeciding] = useState<PendingDecision | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetchApprovals({ status: statusFilter, page: 1, page_size: 50 });
      setItems(resp.items);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载审批列表失败');
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const openDecision = (record: ApprovalItem, approved: boolean) => {
    setComment('');
    setDeciding({ record, approved });
  };

  const submitDecision = async () => {
    if (!deciding) return;
    // 驳回必须写理由——后端会返回 422，前端先挡一层，体验更好
    if (!deciding.approved && !comment.trim()) {
      message.warning('驳回时必须填写理由');
      return;
    }
    setSubmitting(true);
    try {
      const resp = await decideApproval(deciding.record.apply_id, {
        approved: deciding.approved,
        reviewer: REVIEWER,
        comment: comment.trim(),
      });
      if (resp.ok) {
        message.success(deciding.approved ? '已同意' : '已驳回');
        setDeciding(null);
        await load();
      } else {
        // 审批已落库但图恢复失败，如实告诉操作人
        message.warning(resp.warning ?? '审批已记录，但会话恢复失败');
        setDeciding(null);
        await load();
      }
    } catch (error) {
      // 409 = 已经被别人处理过 / 已过期，刷新列表让人看到真实状态
      if (error instanceof ApiRequestError && error.status === 409) {
        message.warning(error.message);
        setDeciding(null);
        await load();
      } else {
        message.error(error instanceof Error ? error.message : '审批提交失败');
      }
    } finally {
      setSubmitting(false);
    }
  };

  const columns: TableProps<ApprovalItem>['columns'] = [
    { title: '审批单号', dataIndex: 'apply_id', width: 210 },
    { title: '会员', dataIndex: 'member_id', width: 100 },
    {
      title: '类型',
      dataIndex: 'type',
      width: 80,
      render: (type: ApprovalItem['type']) => TYPE_TEXT[type] ?? type,
    },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 100,
      render: (amount: number) => `¥${(amount / 100).toFixed(2)}`,
    },
    { title: '摘要', dataIndex: 'summary' },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: ApprovalItem['status']) => {
        const meta = STATUS_META[status] ?? { color: 'default', text: status };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '过期时间',
      dataIndex: 'expires_at',
      width: 150,
      render: (value: string) => dayjs(value).format('MM-DD HH:mm'),
    },
    {
      title: '操作',
      key: 'action',
      width: 140,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            type="primary"
            disabled={record.status !== 'pending'}
            onClick={() => openDecision(record, true)}
          >
            同意
          </Button>
          <Button
            size="small"
            danger
            disabled={record.status !== 'pending'}
            onClick={() => openDecision(record, false)}
          >
            驳回
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card
      title="审批台"
      extra={
        <Space>
          <Segmented<ApprovalStatusFilter>
            value={statusFilter}
            onChange={(v) => setStatusFilter(v)}
            options={[
              { label: '待审批', value: 'pending' },
              { label: '已同意', value: 'approved' },
              { label: '已驳回', value: 'rejected' },
              { label: '全部', value: 'all' },
            ]}
          />
          <Button onClick={() => void load()}>刷新</Button>
        </Space>
      }
      styles={{ body: { padding: 0 } }}
    >
      <Table<ApprovalItem>
        rowKey="apply_id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
        scroll={{ x: 1100 }}
      />

      <Modal
        title={deciding?.approved ? '同意审批' : '驳回审批'}
        open={deciding !== null}
        onCancel={() => setDeciding(null)}
        onOk={() => void submitDecision()}
        confirmLoading={submitting}
        okText="提交"
        okButtonProps={{ danger: deciding ? !deciding.approved : false }}
      >
        {deciding ? (
          <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
            <div>{deciding.record.summary}</div>
            <Input.TextArea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={deciding.approved ? '备注（可选）' : '驳回理由（必填）'}
              autoSize={{ minRows: 2, maxRows: 4 }}
            />
          </Space>
        ) : null}
      </Modal>
    </Card>
  );
}

export default ApprovalPage;
