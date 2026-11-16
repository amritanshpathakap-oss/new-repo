import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const errors = new Rate('order_errors');
export const options = {
  scenarios: { orders: { executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 20), timeUnit: '1s', duration: __ENV.DURATION || '2m', preAllocatedVUs: 100, maxVUs: 300 } },
  thresholds: { http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<500'], order_errors: ['rate<0.01'] },
};
export default function () {
  const res = http.post(`${__ENV.BASE_URL || 'http://localhost:8000'}/orders`, JSON.stringify({sku:'demo', quantity:1}), {headers:{'Content-Type':'application/json'}, timeout:'5s'});
  const ok = check(res, {'accepted': r => r.status === 202});
  errors.add(!ok);
}
