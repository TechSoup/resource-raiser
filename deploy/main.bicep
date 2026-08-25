@description('Globally unique App Service name.')
param appName string

param location string = resourceGroup().location

@minValue(1)
@maxValue(30)
param maxInstances int = 4

@minValue(1)
param minInstances int = 1

@description('Total SEC request budget shared conservatively across the maximum instance count.')
param secFleetRequestsPerSecond int = 8

@minValue(1)
@description('Bootstrap scale-out threshold; replace with a measured value after bounded calibration.')
param scaleOutRequestsPerFiveMinutes int = 100

@minValue(0)
@description('Bootstrap scale-in threshold; the long window exceeds the query timeout.')
param scaleInRequestsPerFifteenMinutes int = 10

@secure()
@description('Runtime app settings. Prefer Key Vault reference strings instead of literal secrets.')
param secretSettings object = {}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  kind: 'linux'
  sku: {
    name: 'P0v3'
    tier: 'PremiumV3'
    capacity: minInstances
  }
  properties: {
    reserved: true
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${appName}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
  }
}

var fixedSettings = {
  APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
  PORT: '8000'
  AGENT_FINDER_BIND_HOST: '127.0.0.1'
  AGENT_FINDER_URL: 'http://127.0.0.1:8088'
  SEARCH_LIMIT_PER_DAY: '0'
  WEBAPP_MAX_INSTANCES: string(maxInstances)
  SEC_FLEET_REQUESTS_PER_SECOND: string(secFleetRequestsPerSecond)
  OTEL_SERVICE_NAME: 'resource-raiser'
  SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
}

// Fixed deployment invariants win over same-named caller settings, without duplicate keys.
var mergedSettings = union(secretSettings, fixedSettings)

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      alwaysOn: true
      healthCheckPath: '/healthz'
      linuxFxVersion: 'PYTHON|3.13'
      appCommandLine: 'bash deploy/start-webapp.sh'
    }
  }
}

resource appSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: site
  name: 'appsettings'
  properties: mergedSettings
}

resource autoscale 'Microsoft.Insights/autoscalesettings@2022-10-01' = {
  name: '${appName}-autoscale'
  location: location
  properties: {
    enabled: true
    targetResourceUri: plan.id
    profiles: [{
      name: 'request-rate'
      capacity: {
        minimum: string(minInstances)
        maximum: string(maxInstances)
        default: string(minInstances)
      }
      rules: [
        {
          metricTrigger: {
            metricName: 'Requests'
            metricResourceUri: site.id
            timeGrain: 'PT1M'
            statistic: 'Average'
            timeWindow: 'PT5M'
            timeAggregation: 'Total'
            operator: 'GreaterThan'
            threshold: scaleOutRequestsPerFiveMinutes
          }
          scaleAction: { direction: 'Increase', type: 'ChangeCount', value: '1', cooldown: 'PT5M' }
        }
        {
          metricTrigger: {
            metricName: 'Requests'
            metricResourceUri: site.id
            timeGrain: 'PT1M'
            statistic: 'Average'
            timeWindow: 'PT15M'
            timeAggregation: 'Total'
            operator: 'LessThan'
            threshold: scaleInRequestsPerFifteenMinutes
          }
          scaleAction: { direction: 'Decrease', type: 'ChangeCount', value: '1', cooldown: 'PT15M' }
        }
      ]
    }]
  }
}

output hostname string = site.properties.defaultHostName
output applicationInsights string = insights.name
