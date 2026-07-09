{{- define "app.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/managed-by: helmsman
{{- end -}}
