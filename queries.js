// Paste the exact queries from Postman (Body > GraphQL) here. The script fills in the variables.
// Variables used: count, cursor, filterConfig, first, offset, id, logCount, logCursor, asc

export const Q = {
  currentUser: `query { currentUser { id email role __typename } }`,

  getJobs: `query getJobs($count: Int, $cursor: String, $filterConfig: JobFilterConfigInput) {
    jobs(first: $count, after: $cursor, filterConfig: $filterConfig) {
      pageInfo { hasNextPage __typename }
      edges { cursor node { id status creationTimestamp deliveryTimestamp retriable retries
        sender { id name description __typename } integration { id name description __typename }
        dataType { id name description __typename } receiver { id name description __typename }
        parents { id __typename } __typename } __typename }
      __typename } }`,

  pendingBatchJobs: `query GetPendingBatchJobs($first: Int, $offset: Int) {
    pendingBatchJobs(first: $first, offset: $offset) {
      hasMore
      items { job { id creationTimestamp sender { id name __typename } receiver { id name __typename }
        dataType { id name __typename } __typename } nextActionTime __typename }
      __typename } }`,

  dataTypes: `query Query($count: Int, $cursor: String, $filterConfig: DataTypeFilterConfigInput) {
    dataTypes(first: $count, after: $cursor, filterConfig: $filterConfig) {
      pageInfo { hasNextPage __typename }
      edges { cursor node { description id name __typename } __typename }
      __typename } }`,

  dataSystems: `query Query($count: Int, $cursor: String, $filterConfig: DataSystemFilterConfigInput) {
    dataSystems(first: $count, after: $cursor, filterConfig: $filterConfig) {
      pageInfo { hasNextPage __typename }
      edges { cursor node { id name __typename } __typename }
      __typename } }`,

  // CHECK THIS ONE: I could only partly read it from the screenshot.
  // The sftpGatewayLog arguments and fields are a guess. Paste the exact query from Postman.
  jobDetails: `query JobDetails($id: String!, $logCount: Int, $logCursor: String, $asc: Boolean) {
    job(id: $id) {
      id status creationTimestamp deliveryTimestamp cancellationTimestamp failureTimestamp fileSize
      scheduledDelivery nextActionTime retriable
      payloadHistory { component payloadUrl __typename }
      parents { id status creationTimestamp __typename }
      sftpGatewayLog(count: $logCount, cursor: $logCursor, asc: $asc) { id __typename }
      sender { id name __typename } integration { id name __typename }
      receiver { id name __typename } dataType { id name __typename }
      __typename } }`,
};
