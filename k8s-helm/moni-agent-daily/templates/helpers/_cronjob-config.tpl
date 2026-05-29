{{- define "deploy-bot.cronjob" -}}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "deploy-bot.fullname" . }}-{{ .name }}
  labels:
    {{- include "deploy-bot.labels" . | nindent 4 }}
spec:
  schedule: {{ .config.schedule | quote }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      template:
        metadata:
          labels:
            {{- include "deploy-bot.selectorLabels" . | nindent 12 }}
        spec:
          restartPolicy: OnFailure
          containers:
            - name: {{ .name }}
              image: "{{ .config.image.repository }}:{{ .config.image.tag | default .Chart.AppVersion }}"
              imagePullPolicy: {{ .config.image.pullPolicy | default "IfNotPresent" }}
              command:
                - python
                - -m
                - metrics_service.daily_report
              {{- with .config.env }}
              env:
                {{- toYaml . | nindent 16 }}
              {{- end }}
              {{- with .config.resources }}
              resources:
                {{- toYaml . | nindent 16 }}
              {{- end }}
          {{- with .config.imagePullSecrets }}
          imagePullSecrets:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.nodeSelector }}
          nodeSelector:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.affinity }}
          affinity:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.tolerations }}
          tolerations:
            {{- toYaml . | nindent 12 }}
          {{- end }}
{{- end }}
