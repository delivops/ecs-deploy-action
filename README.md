[![DelivOps banner](https://raw.githubusercontent.com/delivops/.github/main/images/banner.png?raw=true)](https://delivops.com)

# ECS Deploy Action

This GitHub Action automates the deployment of containerized applications to Amazon ECS (Elastic Container Service). It streamlines the process of updating ECS services with new container images, handling the deployment process in a reliable and efficient manner.

### Key Features

- Automated deployment to Amazon ECS
- Supports task definition updates
- Handles service updates with zero-downtime deployment
- Configurable deployment parameters
- Integration with GitHub Actions workflow

### Example Usage

```yaml
name: Deploy to ECS

on:
  push:
    branches: [main]
    paths:
      - "**"
      - "!.github/**"

jobs:
  deploy:
    name: Deploy
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v3

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v2
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-east-1

      - name: Deploy to ECS
        uses: delivops/ecs-deploy-action@v1
        with:
          cluster: my-ecs-cluster
          service: my-ecs-service
          container-name: my-container
          image: my-ecr-repo:latest
          task-definition: task-definition.json
          aws-region: us-east-1
```
