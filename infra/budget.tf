/**
 * Sprint 8, Parte B -- orcamento com alerta (requisito explicito do
 * sprint: "custo perto de zero fora da janela de demo... orcamento com
 * alerta"). So NOTIFICA -- Cloud Billing Budgets nao interrompe nem
 * limita gasto automaticamente sem uma automacao extra (Cloud Function
 * reagindo ao Pub/Sub do orcamento), fora do escopo deste sprint; nao
 * inventado aqui porque nao foi pedido e seria abstracao especulativa
 * (regra do CLAUDE.md).
 *
 * Notifica os destinatarios IAM padrao (Billing Account Administrators/
 * Billing Account Users, ver `all_updates_rule.disable_default_iam_recipients`
 * default false = NAO desabilita) -- nao criamos um canal de notificacao
 * dedicado (google_monitoring_notification_channel) por simplicidade: o
 * dono da conta de faturamento ja recebe e-mail por padrao.
 *
 * "quota project" -- incidente real neste sprint: `apply` falhou com 403
 * em `billingbudgets.googleapis.com`, e o erro citava
 * `consumer: projects/764086051850` -- um projeto que NAO e o alvo do
 * deploy. Causa: a Billing Budgets API cobra a chamada contra o "quota
 * project" das credenciais ADC locais (`gcloud auth application-default
 * login`), nao contra `var.project_id` -- se a ADC nunca teve um quota
 * project setado explicitamente, o gcloud usa um projeto pessoal
 * default, que quase certamente nao tem essa API habilitada nem
 * permissao de billing. Corrigido rodando (uma vez, fora do Terraform):
 *   gcloud auth application-default set-quota-project <PROJECT_ID>
 * (ver deploy.sh, etapa 1 -- roda isso junto com o `services enable`).
 */

resource "google_billing_budget" "sentinel" {
  billing_account = var.billing_account_id
  display_name    = "sentinel-hackathon-budget"

  depends_on = [google_project_service.required]

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }

  # 50%/90%/100% do orcamento -- alerta cedo o suficiente pra reagir antes
  # de estourar, nao so um aviso de "ja estourou".
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
}
