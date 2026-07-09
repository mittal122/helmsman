{{- define "app.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
helmsman.dev/managed-by: helmsman
{{- end -}}
