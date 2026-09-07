FROM grafana/grafana:11.2.0

# Bake provisioning configs and dashboard into the image.
# On startup Grafana reads these automatically — no manual setup needed.

COPY provisioning/datasources /etc/grafana/provisioning/datasources
COPY provisioning/dashboards  /etc/grafana/provisioning/dashboards
COPY dashboards               /var/lib/grafana/dashboards
