from tools import cost

_DEPLOY = """
apiVersion: apps/v1
kind: Deployment
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: app
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
"""

def test_estimate_from_requests():
    r = cost.estimate(_DEPLOY)
    # 2 replicas x 0.05 vCPU = 0.1 vCPU ; 2 x 64Mi = 0.125 GiB
    exp_cpu = round(0.1 * cost.PRICE["cpu_hour"] * cost.HOURS, 2)
    exp_mem = round(0.125 * cost.PRICE["gb_hour"] * cost.HOURS, 2)
    assert r["breakdown"]["cpu_usd"] == exp_cpu
    assert r["breakdown"]["mem_usd"] == exp_mem
    assert r["monthly_usd"] == round(exp_cpu + exp_mem, 2)

def test_estimate_handles_no_deployment():
    assert cost.estimate("kind: Service")["monthly_usd"] == 0.0

def test_cpu_and_mem_parsers():
    assert cost._cpu("500m") == 0.5 and cost._cpu("2") == 2.0
    assert cost._gib("64Mi") == 0.0625 and cost._gib("1Gi") == 1.0
