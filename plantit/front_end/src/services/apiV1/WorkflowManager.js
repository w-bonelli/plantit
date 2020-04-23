import axios from 'axios';
import * as Sentry from '@sentry/browser';

export default {
    getWorkflows() {
        /**
         * Get Available stroage types.
         *
         * Returns:
         *    Axios promise containing returning an array of workflow objects
         **/
        return axios
            .get('/apis/v1/workflows/')
            .then(response => {
                return response.data.workflows;
            })
            .catch(err => {
                Sentry.captureException(err);
            });
    },
    getWorkflow(workflow) {
        /**
         * Get workflow info and parameters
         *
         * Args:
         *   workflow (str): app_name of workflow
         *
         * Returns:
         *    Axios promise containing returning the workflow info and parameters
         **/
        return axios
            .get(`/apis/v1/workflows/${workflow}/`)
            .then(response => {
                return response.data;
            })
            .catch(err => {
                Sentry.captureException(err);
            });
    },
    submitJob(workflow, pk, params) {
        return axios({
            method: 'post',
            url: `/apis/v1/workflows/${workflow}/submit/${pk}/`,
            data: params,
            headers: { 'Content-Type': 'application/json' }
        }).catch(err => {
            Sentry.captureException(err);
        });
    }
};
