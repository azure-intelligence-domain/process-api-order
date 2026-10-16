// A building block's Function on Flex Consumption (pay per execution, free grant per month), run as
// the block's runtime identity: no keys, no connection strings with secrets. Behind Easy Auth, so
// only the given gateway identities get in: nobody goes around the gateway (ADR 0014).
// Reusable as is by every Function building block.

param name string
param location string
param tags object

@description('Resource id of the runtime identity (id-intplat-<block>-runtime).')
param runtimeIdentityId string
param runtimeClientId string
param runtimePrincipalId string

@description('Application Insights connection string (appi-intplat-demo): one trace per journey.')
param appInsightsConnectionString string

@description('Application (client) id of intplat-platform-apis: the audience the gateways present.')
param platformApisAppId string

@description('Principal ids allowed in: the gateways\' managed identities.')
param allowedPrincipalIds array

@description('Extra app settings for the code.')
param appSettings object = {}

var blobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageName = 'st${take(replace(replace(name, 'func-intplat-', ''), '-', ''), 9)}${uniqueString(resourceGroup().id)}'

resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource deployments 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  name: '${storage.name}/default/deployments'
}

resource runtimeOwnsStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, runtimePrincipalId, blobDataOwner)
  scope: storage
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataOwner)
    description: '${name}: deployment package and host storage.'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'asp-${replace(name, 'func-', '')}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: name
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${runtimeIdentityId}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}deployments'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: runtimeIdentityId
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 512
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat(
        [
          { name: 'AzureWebJobsStorage__accountName', value: storage.name }
          { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
          { name: 'AzureWebJobsStorage__clientId', value: runtimeClientId }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
          { name: 'AZURE_CLIENT_ID', value: runtimeClientId }
        ],
        map(items(appSettings), s => { name: s.key, value: s.value })
      )
    }
  }
  dependsOn: [deployments, runtimeOwnsStorage]
}

resource easyAuth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: platformApisAppId
          openIdIssuer: 'https://sts.windows.net/${tenant().tenantId}/'
        }
        validation: {
          allowedAudiences: ['api://${platformApisAppId}']
          defaultAuthorizationPolicy: {
            allowedPrincipals: { identities: allowedPrincipalIds }
          }
        }
      }
    }
    login: {
      tokenStore: { enabled: false }
    }
  }
}

output name string = site.name
output defaultHostName string = site.properties.defaultHostName
