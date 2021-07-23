#!/bin/bash

eval "$(ssh-agent -s)" # Start ssh-agent cache
chmod 600 ~/.ssh/id_rsa # Allow read access to the private key
ssh-add ~/.ssh/id_rsa # Add the private key to SSH

echo "Deploying production"
ssh -o StrictHostKeyChecking=no -p $PROD_PORT "$PROD_USER@$PROD_HOST" <<EOF
  cd $PROD_DIRECTORY
  chmod +x scripts/deploy-prod.sh
  scripts/deploy.sh $PROD_HOST $PROD_EMAIL
EOF