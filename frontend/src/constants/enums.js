export const Role = Object.freeze({ VIEWER: 'viewer', ANALYST: 'analyst', SUPERVISOR: 'supervisor' });
export const RunStatus = Object.freeze({ QUEUED: 'queued', SUCCEEDED: 'succeeded', FAILED: 'failed' });
export const Decision = Object.freeze({ APPROVED: 'approved', RETURNED: 'returned' });
export const AdjustmentStatus = Object.freeze({ PENDING: 'pending', SUBMITTED: 'submitted', SETTLED: 'settled' });
export const roleName = {viewer: '只读查看', analyst: '业务分析员', supervisor: '复核负责人'};
export const statusName = {queued: '等待计算', succeeded: '等待复核', failed: '计算失败', approved: '已签核', returned: '已退回',
  pending: '待结', submitted: '已交结算员', settled: '已结清', success: '结账成功', failed: '结账失败'};
export const bandName = {valley: '谷段', flat: '平段', peak: '峰段'};
