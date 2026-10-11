// 원화(KRW)는 소수 단위가 없는 통화다 — 상품 가격·주문 금액 모두 원 단위 정수로 저장한다.
export function formatWon(value) {
  const n = Math.round(Number(value) || 0);
  return `₩${n.toLocaleString('ko-KR')}`;
}
