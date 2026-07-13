import pytest
from tools import compose

def test_parses_multiservice_with_dns_order_and_secret_split():
    svcs, warns = compose.parse("""
services:
  web:
    image: nginx:1.27
    ports: ["8080:80"]
    depends_on: [db]
    environment: {APP_ENV: prod, API_TOKEN: t0k}
  db:
    image: postgres:16
    environment: {POSTGRES_PASSWORD: pw}
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck: {test: ["CMD", "pg_isready"]}
""")
    assert [s["name"] for s in svcs] == ["db", "web"]        # depends_on order
    web = next(s for s in svcs if s["name"] == "web")
    db = next(s for s in svcs if s["name"] == "db")
    assert web["port"] == 80 and web["published"] is True
    assert web["env"] == {"APP_ENV": "prod"} and web["secrets"] == {"API_TOKEN": "t0k"}
    assert db["published"] is False                          # no ports:
    assert db["probe"] == {"type": "exec", "command": ["pg_isready"]}
    assert db["volumes"] == [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data", "size": "1Gi"}]

def test_no_healthcheck_with_port_gets_tcp_probe():
    svcs, _ = compose.parse("services:\n  cache:\n    image: redis:7\n    ports: ['6379']\n")
    assert svcs[0]["probe"] == {"type": "tcp"}

def test_build_service_produces_build_spec():
    svcs, _ = compose.parse("services:\n  api:\n    build: ./api\n    ports: ['8000:8000']\n")
    assert svcs[0]["image"] == "" and svcs[0]["build"]["subdir"] == "api"
    # long form with an explicit dockerfile keeps root context
    b2, _ = compose.parse("services:\n  api:\n    build:\n      context: .\n      dockerfile: api/Dockerfile\n    ports: ['80']\n")
    assert b2[0]["build"]["dockerfile"] == "api/Dockerfile" and b2[0]["build"]["subdir"] == ""

def test_depends_on_cycle_rejected():
    with pytest.raises(ValueError):
        compose.parse("services:\n  a:\n    image: i\n    depends_on: [b]\n"
                      "  b:\n    image: i\n    depends_on: [a]\n")

def test_invalid_service_name_rejected():
    with pytest.raises(ValueError):
        compose.parse("services:\n  Bad_Name:\n    image: i\n")

def test_networks_and_restart_warn_and_skip():
    svcs, warns = compose.parse("""
services:
  a:
    image: i
    restart: always
    networks: [back]
networks:
  back: {}
""")
    assert any("networks" in w for w in warns)
    assert any("restart" in w for w in warns)

def test_resources_and_user_and_command_mapped():
    svcs, _ = compose.parse("""
services:
  w:
    image: i
    user: "1001:1001"
    command: ["python", "app.py"]
    entrypoint: /entry.sh
    deploy:
      replicas: 3
      resources:
        limits: {cpus: "0.5", memory: 512M}
""")
    s = svcs[0]
    assert s["run_as_user"] == 1001 and s["replicas"] == 3
    assert s["args"] == ["python", "app.py"]                 # compose command -> args
    assert s["command"] == ["/entry.sh"]                     # compose entrypoint -> command
    assert s["resources"]["limits"] == {"cpu": "500m", "memory": "512Mi"}

def test_volume_name_sanitized_to_rfc1123():
    # compose 'postgres_data' (underscore) is illegal as a K8s PVC/volume name -> coerced
    svcs, _ = compose.parse("services:\n  db:\n    image: postgres:16\n"
                            "    volumes: ['postgres_data:/var/lib/postgresql/data']\n")
    assert svcs[0]["volumes"][0]["name"] == "postgres-data"
    assert svcs[0]["volumes"][0]["mountPath"] == "/var/lib/postgresql/data"

def test_interpolation_defaults_and_provided_and_required():
    text, warns = compose.interpolate(
        "img: repo:${TAG:-v1.0.0}\npw: ${DB_PASS:?need it}\nplain: $HOME_X\nlit: $$KEEP\n",
        {"DB_PASS": "s3cret"})
    assert "repo:v1.0.0" in text          # default used when unset
    assert "pw: s3cret" in text           # provided value used
    assert "lit: $KEEP" in text           # $$ -> literal $
    assert "plain: \n" in text            # unset -> empty (+ warned)
    assert any("HOME_X" in w for w in warns)

def test_interpolation_resolves_image_tag_in_parse():
    svcs, warns = compose.parse(
        "services:\n  api:\n    image: org/api:${TAG:-v2}\n", {"TAG": "v9"})
    assert svcs[0]["image"] == "org/api:v9"
    # and the default path when not provided
    svcs2, _ = compose.parse("services:\n  api:\n    image: org/api:${TAG:-v2}\n")
    assert svcs2[0]["image"] == "org/api:v2"

def test_malformed_yaml_rejected():
    with pytest.raises(ValueError):
        compose.parse("services: [this is: not valid")
