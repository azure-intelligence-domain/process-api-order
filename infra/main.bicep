// [Order] Order Process API: a Python Function on Flex Consumption, in rg-intplat-process-api-order.
// It runs as its runtime identity: calls the Salesforce System API through the gateway, publishes
// to evgt-intplat-order (grants: integration-platform/routing). Both gateways may call it.

param location string = 'belgiumcentral'

@description('Application (client) id of the intplat-platform-apis Entra application (ADR 0014).')
param platformApisAppId string = 'e3337087-144a-4b2e-8faa-c802660b9dee'

var tags = {
  project: 'ai-intelligence-domain'
  component: 'process-api-order'
  owner: 'order-team'
}

resource runtime 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-intplat-process-api-order-runtime'
  scope: resourceGroup('rg-intplat-identities')
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-intplat-demo'
  scope: resourceGroup('rg-intplat-platform')
}

resource peopleGateway 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: 'apim-intplat-demo'
  scope: resourceGroup('rg-intplat-platform')
}

resource agentGateway 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: 'apim-intplat-agents'
  scope: resourceGroup('rg-intplat-platform')
}

resource orderTopic 'Microsoft.EventGrid/topics@2025-02-15' existing = {
  name: 'evgt-intplat-order'
  scope: resourceGroup('rg-intplat-platform')
}

module function 'modules/function-app.bicep' = {
  name: 'function'
  params: {
    name: 'func-intplat-process-api-order'
    location: location
    tags: tags
    runtimeIdentityId: runtime.id
    runtimeClientId: runtime.properties.clientId
    runtimePrincipalId: runtime.properties.principalId
    appInsightsConnectionString: appInsights.properties.ConnectionString
    platformApisAppId: platformApisAppId
    allowedPrincipalIds: [peopleGateway.identity.principalId, agentGateway.identity.principalId]
    appSettings: {
      SALESFORCE_API_URL: '${peopleGateway.properties.gatewayUrl}/salesforce'
      PLATFORM_APIS_AUDIENCE: 'api://${platformApisAppId}'
      ORDER_TOPIC_ENDPOINT: orderTopic.properties.endpoint
    }
  }
}

output functionName string = function.outputs.name
output hostName string = function.outputs.defaultHostName
