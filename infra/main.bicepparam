using './main.bicep'

param namePrefix = 'a365factory'
param tags = {
  environment: 'dev'
  workload: 'agent-365-factory'
}
