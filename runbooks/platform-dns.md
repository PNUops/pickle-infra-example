# 런북: 플랫폼 도메인 DNS 검증과 복구

플랫폼 호스트에서 실행한다. VM에 연결한 PLATFORM/AUTO 이름은 API가 개별 A를 만들고,
vhost 해제 적용을 확인한 뒤 그 A를 삭제한다. EXTERNAL 이름의 레코드는 소유자가 정한다.
DB의 이름 행이나 승인 대기 신청만으로 DNS 레코드가 생기지 않는다.

와일드카드 A와 `*.<root>` 인증서는 별도 설정이다. 개별 A 구조에서는 DNS wildcard를
생성하지 않는다. Let's Encrypt 와일드카드 lineage와 DNS-01 계정, `_acme-challenge` TXT,
CAA, 갱신 타이머, nginx reload hook, 인증서 DB 행 갱신은 유지한다.

## 헬스체크 설정

`/etc/pickle/host.env`에 해당 환경의 값을 기록한다. NS는 managed zone이 실제로 가진
전체 집합을 공백으로 구분하고 따옴표로 감싼다. 다른 존도 자기 NS 집합을 사용한다.

```bash
PLATFORM_ROOT_DOMAIN="example.dev"
PLATFORM_DNS_MODE="explicit"
PLATFORM_DNS_EXPECTED_NS="ns1.example.test ns2.example.test ns3.example.test ns4.example.test"
PLATFORM_DNS_MANUAL_FQDNS="staging.example.dev"
MAIN_DOMAIN_PUBLIC_IP="203.0.113.10"
```

`PLATFORM_DNS_MANUAL_FQDNS`는 DB 외부에서 운영자가 관리하는 ingress 이름 목록이다.
해당 이름이 없으면 빈 문자열로 둔다. `PLATFORM_DNS_PROBE_FQDN`은 재현용 미등록 이름을
지정할 때만 사용한다. 기본값은 매 실행마다 새 이름이며 소유자 레코드를 만들지 않는다.

`explicit`은 기본 모드다. 정확한 NS 위임을 대조한 뒤 각 권위 NS에서 미등록 이름의
NXDOMAIN, apex A, 수동 ingress 이름과 ACTIVE·미해제 PLATFORM/AUTO의 APPLIED route
이름을 확인한다. 개별 A는 공인 ingress IP와 정확히 일치해야 한다. 등록 이름이 0개면
그 대상만 SKIP이다. DB 조회 실패는 SKIP이 아니며 DNS FAILED 행은 별도 FAIL이다.
timeout, SERVFAIL, NOERROR/NODATA는 미등록 이름 NXDOMAIN의 성공으로 취급하지 않는다.

`wildcard`는 전환 중이나 롤백으로 wildcard A를 의도적으로 복구한 경우에만 사용한다.
이 모드는 미등록 이름이 ingress IP로 해석돼야 한다. wildcard가 있으면 등록 이름 A
응답도 합성될 수 있으므로 개별 RRset의 존재를 증명하지 못한다. 개별 A 선행 대조는
Cloud DNS API의 정확한 owner/type 목록으로 수행한다.

직접 실행할 때도 타이머와 같은 환경을 읽는다.

```bash
set -a
. /etc/pickle/host.env
set +a
bash /srv/pickle/infra/scripts/health-check.sh
```

## 새 환경과 복구

1. DNS 존의 NS, SOA, CAA, apex A와 수동 ingress 이름을 먼저 확인한다. 수동 vhost와
   stream SNI map도 함께 조사한다. DB만 읽으면 staging 같은 운영자 이름을 놓칠 수 있다.
2. API가 사용하는 DNS 제공자와 계정 가독성을 확인한다. serving PLATFORM/AUTO의
   개별 A 누락은 정확한 owner/type로 대조하고 복구한다. EXTERNAL 레코드는 별도 보존한다.
   전체 resync는 vhost를 먼저 적용할 수 있으므로 그 성공을 DNS 복구 증거로 삼지 않는다.
3. 인증서 lineage와 root certRef를 확인한다. 프록시 파일을 복원해도 Cloud DNS는 복원되지
   않는다. DB를 복구해도 DNS 전체를 DB에서 재생성할 수 없다.
4. `curl --resolve <이름>:443:<proxy-IP> https://<이름>/`으로 SNI와 인증서 경로를 확인하고,
   외부에서 일반 DNS를 사용한 HTTPS도 따로 검증한다. 미등록 SNI·Host 거부는 IP를 직접
   지정해 확인한다. `.dev` 브라우저는 HTTPS를 요구한다.

## wildcard A 제거와 롤백

실행 전에 전체 zone export와 제거할 정확한 RRset·TTL을 보호 백업 경로에 보관한다.
수동 ingress와 serving 이름의 개별 A를 선행 생성하고, 현재 사용자 신청은 사용하지 않는다.
검증용 이름은 제거 전 발행해 유지하고, 제거 후 DNS/TLS를 확인한 다음 해제해 A와 vhost
삭제까지 검증한다. 전환 전에는 `wildcard`, 삭제 직후에는 `explicit`으로 판정한다.

제거는 wildcard A RRset 하나만 대상으로 한다. apex, CAA, NS, SOA와 DNS-01 경로는 보존한다.
장애 시 백업의 wildcard A 한 세트를 동일 owner, 주소, TTL로 다시 생성하고 `wildcard`
모드로 되돌린다. 전체 zone import로 다른 사용자의 신규 레코드를 덮어쓰지 않는다.
권위 NS 복구 뒤에도 positive/negative cache의 TTL 동안 사용자 결과가 다를 수 있다.

레코드가 없는 이름은 NXDOMAIN, TXT/AAAA 또는 하위 레코드만 존재하는 이름의 A는
NOERROR/NODATA가 될 수 있다. DNS-01 dry-run은 TXT를 쓰며 deploy hook 검증은 nginx
reload를 포함하므로 승인된 실행 범위에서 수행한다. timer success가 실제 갱신을 뜻하지는
않는다. 인증서 DB 행과 설치 leaf의 만료일도 별도로 대조한다.
