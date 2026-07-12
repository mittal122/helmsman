{{- define "app.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
helmsman.dev/managed-by: helmsman
{{- with .Values.stack }}
helmsman.dev/stack: {{ . }}
{{- end }}
{{- end -}}

{{- /* probe handler: http (default) renders exactly as before; tcp/exec for non-HTTP
       services (databases etc.) so a DB isn't killed by an HTTP liveness probe. */ -}}
{{- define "app.probeHandler" -}}
{{- $p := .Values.probe | default dict -}}
{{- $type := $p.type | default "http" -}}
{{- if eq $type "http" -}}
httpGet:
  path: {{ $p.path | default .Values.probePath | quote }}
  port: {{ .Values.port }}
{{- else if eq $type "tcp" -}}
tcpSocket:
  port: {{ .Values.port }}
{{- else if eq $type "exec" -}}
exec:
  command:
{{- range $p.command }}
    - {{ . | quote }}
{{- end -}}
{{- end -}}
{{- end -}}
