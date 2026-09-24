// Container App do agente. Provisiona apenas o runtime — blueprint e Entra Agent ID
// são criados pelo a365 CLI, e o `a365 deploy app` NÃO é usado aqui porque ele
// publica em App Service, não em Container Apps.
//
// As tags não são decoração: são o inventário. Dono, propósito e data de revisão
// precisam ser respondíveis por consulta ao Azure Resource Graph, sem depender do
// repositório.

@description('Nome do Container App')
param containerAppName string

@description('Slug do agente (vem de requests/<agente>.yaml)')
param agentName string

@description('Nome de exibição do agente')
param displayName string

@description('dev | prod')
param environmentTag string

@description('E-mail do dono de negócio')
param businessOwner string

@description('E-mail do dono técnico')
param technicalOwner string

@description('publico | interno | confidencial | restrito')
param dataSensitivity string

@description('Data em que o dono precisa reconfirmar o agente (ISO)')
param reviewDate string

@description('Imagem completa, ex: acr.azurecr.io/classificador:sha')
param image string

@description('Resource ID do Container Apps Environment existente')
param managedEnvironmentId string

@description('Login server do ACR, ex: acra365agents.azurecr.io')
param acrLoginServer string

@description('Resource ID da managed identity usada para puxar a imagem e ler o Key Vault')
param userAssignedIdentityId string

@description('Client ID da mesma managed identity. DefaultAzureCredential só usa a identidade atribuída pelo usuário se AZURE_CLIENT_ID estiver no ambiente.')
param userAssignedIdentityClientId string

@description('ID do tenant do A365')
param a365TenantId string

@description('ID da instância do agente (Entra Agent ID), criado por `a365 create-instance identity`')
param a365AgentInstanceId string

@description('Client ID do blueprint do agente')
param a365BlueprintClientId string

type purviewConfiguration = {
  enabled: bool
  agentUserId: string
  applicationId: string
  checkOutput: bool
}

@description('Protecao Purview centrada no agente. Habilitar somente apos validar identidade, permissoes e politicas inline no tenant.')
param purview purviewConfiguration = {
  enabled: false
  agentUserId: ''
  applicationId: ''
  checkOutput: false
}

@description('URI do segredo do blueprint no Key Vault (secretRef). Nunca passar o segredo em texto.')
@secure()
param blueprintSecretKeyVaultUri string

@description('Endpoint do Azure OpenAI')
param azureOpenAiEndpoint string

@description('Nome do deployment do Azure OpenAI')
param azureOpenAiDeployment string

@description('Expõe o agente na internet. Padrão false: em produção quem chama é o gateway MCP governado. Use true apenas no lab, para conseguir testar de fora.')
param externalIngress bool = false

param location string = resourceGroup().location

resource agentApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  tags: {
    'a365-agent-name': agentName
    'a365-display-name': displayName
    'a365-environment': environmentTag
    'a365-business-owner': businessOwner
    'a365-technical-owner': technicalOwner
    'a365-data-sensitivity': dataSensitivity
    'a365-review-date': reviewDate
    'a365-managed-by': 'agent-factory'
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: externalIngress
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: userAssignedIdentityId
        }
      ]
      secrets: [
        {
          name: 'blueprint-client-secret'
          keyVaultUrl: blueprintSecretKeyVaultUri
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'A365_TENANT_ID', value: a365TenantId }
            { name: 'A365_AGENT_INSTANCE_ID', value: a365AgentInstanceId }
            { name: 'A365_BLUEPRINT_CLIENT_ID', value: a365BlueprintClientId }
            { name: 'A365_BLUEPRINT_CLIENT_SECRET', secretRef: 'blueprint-client-secret' }
            { name: 'PURVIEW_ENABLED', value: string(purview.enabled) }
            { name: 'PURVIEW_AGENT_USER_ID', value: purview.agentUserId }
            { name: 'PURVIEW_APPLICATION_ID', value: empty(purview.applicationId) ? a365AgentInstanceId : purview.applicationId }
            { name: 'PURVIEW_CHECK_OUTPUT', value: string(purview.checkOutput) }
            { name: 'AZURE_CLIENT_ID', value: userAssignedIdentityClientId }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: environmentTag == 'prod' ? 1 : 0
        maxReplicas: 3
      }
    }
  }
}

output fqdn string = agentApp.properties.configuration.ingress.fqdn
output principalId string = agentApp.identity.type
