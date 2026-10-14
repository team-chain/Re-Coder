// 전자상거래법에 따라 사이트 하단에 표시해야 하는 사업자 정보.
// 운영 전에 아래 값을 실제 정보로 바꾸세요(0 으로 채운 번호는 자리표시입니다).
const BUSINESS = {
  name: '내 상점',
  owner: '홍길동',
  registration: '000-00-00000',
  mailOrder: '제0000-서울○○-0000호',
  address: '서울특별시 ○○구 ○○로 00',
  phone: '1588-0000',
  email: 'help@example.com',
  privacyOfficer: '홍길동',
};

export default function Footer() {
  const b = BUSINESS;
  return (
    <footer className="footer">
      <div className="footer-inner">
        <strong>{b.name}</strong>
        <span>
          대표 {b.owner} · 사업자등록번호 {b.registration} · 통신판매업 신고 {b.mailOrder}
        </span>
        <span>
          주소 {b.address} · 고객센터 {b.phone} · {b.email} · 개인정보보호책임자 {b.privacyOfficer}
        </span>
        <span>교환·반품은 상품을 받은 날로부터 7일 이내 신청할 수 있습니다.</span>
      </div>
    </footer>
  );
}
