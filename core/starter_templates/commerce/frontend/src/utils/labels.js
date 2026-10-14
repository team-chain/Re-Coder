// 주문 상태 값(paid 등)은 서버·결제 웹훅과 맞물려 있으므로 그대로 두고, 화면에 보이는 이름만 바꾼다.
const STATUS_LABEL = {
  unpaid: '결제 전',
  pending: '결제 대기',
  paid: '결제 완료',
  failed: '결제 실패',
  cancelled: '주문 취소',
};

export function statusLabel(status) {
  return STATUS_LABEL[status] || status;
}

export function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('ko-KR', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
