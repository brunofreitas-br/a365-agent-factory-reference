// Console de agentes: a única superfície com ingress externo da fábrica.
// A autenticação é configurada fora daqui (`az containerapp auth`), porque exige o
// FQDN — que só existe depois deste deploy.

param containerAppName string = 'ca-agent-console'
param managedEnvironmentId string
param image string
param acrLoginServer string
param userAssignedIdentityId string
param userAssignedIdentityClientId string
param subscriptionId string = subscription().subscriptionId
param resourceGroupName string = resourceGroup().name
param location string = resourceGroup().location

resource console 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  tags: {
    // Mantém o console fora da descoberta que seleciona apenas runtimes de agentes.
    'a365-managed-by': 'agent-factory-console'
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'console'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AZURE_CLIENT_ID', value: userAssignedIdentityClientId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: resourceGroupName }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        // Mínimo 1: se o console dormir, a tela de login demora e parece quebrada.
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
}

output fqdn string = console.properties.configuration.ingress.fqdn
