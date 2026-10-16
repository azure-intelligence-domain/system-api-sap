// SAP System API: a Python Function on Flex Consumption, in rg-intplat-system-api-sap. It reads and
// writes the simulated SAP (Cosmos DB in backend-systems) as its runtime identity, publishes
// finance facts to evgt-intplat-finance, and only API Management's people gateway can call it.

param location string = 'belgiumcentral'

@description('Application (client) id of the intplat-platform-apis Entra application (ADR 0014).')
param platformApisAppId string = 'e3337087-144a-4b2e-8faa-c802660b9dee'

var tags = {
  project: 'ai-intelligence-domain'
  component: 'system-api-sap'
  owner: 'finance-team'
}

resource runtime 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-intplat-system-api-sap-runtime'
  scope: resourceGroup('rg-intplat-identities')
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-intplat-demo'
  scope: resourceGroup('rg-intplat-platform')
}

// System APIs are served by the people gateway only (product system-apis).
resource gateway 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: 'apim-intplat-demo'
  scope: resourceGroup('rg-intplat-platform')
}

resource topic 'Microsoft.EventGrid/topics@2025-02-15' existing = {
  name: 'evgt-intplat-finance'
  scope: resourceGroup('rg-intplat-platform')
}

module function 'modules/function-app.bicep' = {
  name: 'function'
  params: {
    name: 'func-intplat-system-api-sap'
    location: location
    tags: tags
    runtimeIdentityId: runtime.id
    runtimeClientId: runtime.properties.clientId
    runtimePrincipalId: runtime.properties.principalId
    appInsightsConnectionString: appInsights.properties.ConnectionString
    platformApisAppId: platformApisAppId
    allowedPrincipalIds: [gateway.identity.principalId]
    appSettings: {
      SYSTEM_ENDPOINT: 'https://cosmos-intplat-sap.documents.azure.com:443/'
      SYSTEM_DATABASE: 'sap'
      TOPIC_ENDPOINT: topic.properties.endpoint
    }
  }
}

output functionName string = function.outputs.name
output hostName string = function.outputs.defaultHostName
