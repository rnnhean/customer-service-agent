import { Layout, Menu } from 'antd';
import { AuditOutlined, MessageOutlined } from '@ant-design/icons';
import { useLocation, useNavigate, Routes, Route } from 'react-router-dom';
import ChatPage from '@/pages/ChatPage';
import ApprovalPage from '@/pages/ApprovalPage';
import TracePage from '@/pages/TracePage';

const { Header, Content } = Layout;

const NAV_ITEMS = [
  { key: '/', icon: <MessageOutlined />, label: '对话' },
  { key: '/approvals', icon: <AuditOutlined />, label: '审批台' },
];

function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const selectedKey = location.pathname.startsWith('/approvals') ? '/approvals' : '/';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <div style={{ color: '#fff', fontSize: 16, fontWeight: 600, whiteSpace: 'nowrap' }}>
          客服 Agent
        </div>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selectedKey]}
          items={NAV_ITEMS}
          onClick={({ key }) => navigate(key)}
          style={{ flex: 1, minWidth: 0 }}
        />
      </Header>
      <Content style={{ padding: 16 }}>
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/approvals" element={<ApprovalPage />} />
          <Route path="/traces/:threadId" element={<TracePage />} />
        </Routes>
      </Content>
    </Layout>
  );
}

export default App;
