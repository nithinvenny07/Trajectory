import os
import cv2
import time
import argparse
import torch
import warnings
import numpy as np
import matplotlib.pyplot as plt
import sys
sys.path.append('myDiverseDepth')

from detector import build_detector
from deep_sort import build_tracker
from utils.draw import draw_boxes
from utils.parser import get_config
from utils.log import get_logger
from utils.io import write_results
from Trajectory import individual_TF
from Trajectory.transformer.batch import subsequent_mask

# imports for Diverse Depth
from myDiverseDepth.tools.parse_arg_test import TestOptions
from myDiverseDepth.lib.models.diverse_depth_model import RelDepthModel
from myDiverseDepth.lib.utils.net_tools import load_ckpt
from myDiverseDepth.lib.core.config import cfg, merge_cfg_from_file
from myDiverseDepth.lib.utils.logging import setup_logging, SmoothedValue

import torchvision.transforms as transforms
logger = setup_logging(__name__)




from myDiverseDepth.test_diversedepth_png import get_depth
if torch.cuda.is_available():
    device = torch.device('cuda')
else:
    device = torch.device('cpu')

fps = 0
class VideoTracker(object):
    def __init__(self, cfg , args , video_path):
        self.cfg = cfg
        self.args = args
        self.video_path = video_path
        self.logger = get_logger("root")

        use_cuda = args.use_cuda and torch.cuda.is_available()
        if not use_cuda:
            warnings.warn("Running in cpu mode which maybe very slow!", UserWarning)

        if args.display:
            cv2.namedWindow("test", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("test", args.display_width, args.display_height)

        if args.cam != -1:
            print("Using webcam " + str(args.cam))
            self.vdo = cv2.VideoCapture(args.cam)
        else:
            self.vdo = cv2.VideoCapture()

        #print(fps)
        self.detector = build_detector(cfg, use_cuda=use_cuda)
        self.deepsort = build_tracker(cfg, use_cuda=use_cuda)
        self.traj_ped = individual_TF.IndividualTF(2, 3, 3, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1, mean=[0,0], std=[0,0]).to(device)
        self.traj_ped.load_state_dict(torch.load(f'Trajectory/models/Individual/eth_train/00013.pth', map_location=torch.device('cpu')))
        self.traj_ped.eval()
        self.traj_endeffector = individual_TF.IndividualTF(2, 3, 3, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1, mean=[0,0], std=[0,0]).to(device)
        self.traj_endeffector.load_state_dict(torch.load(f'Trajectory/models/Individual/traj_endeffector.pth', map_location=torch.device('cpu')))
        self.traj_endeffector.eval()
        self.traj_arm = individual_TF.IndividualTF(2, 3, 3, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1, mean=[0, 0], std=[0, 0]).to(device)
        self.traj_arm.load_state_dict(torch.load(f'Trajectory/models/Individual/traj_arm.pth', map_location=torch.device('cpu')))
        self.traj_arm.eval()
        self.traj_probe_holder = individual_TF.IndividualTF(2, 3, 3, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1, mean=[0, 0], std=[0, 0]).to(device)
        self.traj_probe_holder.load_state_dict(torch.load(f'Trajectory/models/Individual/traj_probe_holder.pth', map_location=torch.device('cpu')))
        self.traj_probe_holder.eval()
        self.traj_probe = individual_TF.IndividualTF(2, 3, 3, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1,mean=[0, 0], std=[0, 0]).to(device)
        self.traj_probe.load_state_dict(torch.load(f'Trajectory/models/Individual/traj_probe.pth', map_location=torch.device('cpu')))
        self.traj_probe.eval()
        self.class_names = self.detector.class_names
        self.Q = { }
        # loading model for depth predictin
        test_args = TestOptions().parse()
        test_args.thread = 1
        test_args.batchsize = 1
        merge_cfg_from_file(test_args)
        # load model
        self.model = RelDepthModel()
        self.model.eval()
        # load checkpoint
        if test_args.load_ckpt:
            load_ckpt(test_args, self.model)

        # model.cuda()
        self.model = torch.nn.DataParallel(self.model)

    def __enter__(self):
        if self.args.cam != -1:
            ret, frame = self.vdo.read()
            assert ret, "Error: Camera error"
            self.im_width = frame.shape[0]
            self.im_height = frame.shape[1]

        else:
            assert os.path.isfile(self.video_path), "Path error"
            self.vdo.open(self.video_path)
            self.im_width = int(self.vdo.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.im_height = int(self.vdo.get(cv2.CAP_PROP_FRAME_HEIGHT))
            assert self.vdo.isOpened()

        if self.args.save_path:
            os.makedirs(self.args.save_path, exist_ok=True)

            # path of saved video and results
            self.save_video_path = os.path.join(self.args.save_path, "results.avi")
            self.save_results_path = os.path.join(self.args.save_path, "results.txt")

            # create video writer
            #print(self.vdo.get(cv2.CAP_PROP_FPS))
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = cv2.VideoWriter(self.save_video_path, fourcc, self.vdo.get(cv2.CAP_PROP_FPS), (self.im_width, self.im_height))

            # logging
            self.logger.info("Save results to {}".format(self.args.save_path))

        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type:
            print(exc_type, exc_value, exc_traceback)

    def run(self):
        results = []
        pr = []
        obs = []
        idx_frame = 0
        mean_end_effector = torch.tensor((-2.6612e-05, -7.8652e-05))
        std_end_effector = torch.tensor((0.0025, 0.0042))
        mean_arm = torch.tensor([-1.3265e-05, -6.5026e-06])
        std_arm = torch.tensor([0.0030, 0.0185])
        mean_probe_holder = torch.tensor([-5.1165e-05, -7.1806e-05])
        std_probe_holder = torch.tensor([0.0038, 0.0185])
        mean_ped = torch.tensor([0.0001, 0.0001])
        std_ped = torch.tensor([0.0001, 0.0001])
        mean_probe = torch.tensor([0.0005, -0.0015])
        std_probe = torch.tensor([0.0061, 0.0125])
        #window = [0.735156, 0.520270, 0.071875, 0.127027]
        window = [0.755156, 0.560270, 0.075875, 0.157027]
        window[0] = window[0] * 1920
        window[1] = window[1] * 1080
        window[2] = window[2] * 1920
        window[3] = window[3] * 1080
        window[0] = window[0] - window[2] / 2.
        window[2] = window[0] + window[2] / 2.
        window[1] = window[1] - window[3] / 2.
        window[3] = window[1] + window[3] / 2.
        names = ["End-effector", "arm", "probe_holder", "Person", "probe", "window"]
        while self.vdo.grab() :

            idx_frame += 1
            start = time.time()
            _, ori_im = self.vdo.retrieve()
            im = cv2.cvtColor(ori_im, cv2.COLOR_BGR2RGB)
            im_for_depth = im
            # cv2.imshow("frame",im_for_depth)
            # cv2.waitKey(0)
            # plt.imshow(im_for_depth)
            # plt.show()
            height, width = ori_im.shape[:2]
            bbox_xywh , cls_conf, cls_ids = self.detector(im)
            print("class confidence")
            print(cls_conf)
            print(cls_ids)
            boxes_to_print = bbox_xywh.copy()
            if idx_frame % self.args.frame_interval == 0:
                pr = []
                obs = []
                for i in range(5):
                    if i == 3:
                        continue # 3 is for person class so neglecting here
                    mask = cls_ids == i
                    t_cls_conf = cls_conf[mask]
                    t_bbox_xywh = bbox_xywh[mask]
                    if t_cls_conf.size > 0:
                        pt = [t_bbox_xywh[np.argmax(t_cls_conf)][0] / width, t_bbox_xywh[np.argmax(t_cls_conf)][1] / height]
                        t_id = i
                        if t_id in self.Q:
                            self.Q[t_id][0].append(pt)
                        else:
                            self.Q[t_id] = [[pt]]
                # select person class
                mask = cls_ids == 3
                bbox_xywh = bbox_xywh[mask]
                # bbox dilation just in case bbox too small, delete this line if using a better pedestrian detector
                #bbox_xywh[:, 3:] *= 1.2
                cls_conf = cls_conf[mask]

                # do tracking
                outputs = []
                outputs = self.deepsort.update(bbox_xywh, cls_conf, im)
                print(outputs)
                for i in range(len(outputs)):
                    t_id = outputs[i][4]+5 # added with 5 so that ped id will not clash with id's of end_effector arm and probe
                    pt = [(int(outputs[i][0]) + int(outputs[i][2])) / (2*width), (int(outputs[i][1]) + int(outputs[i][3])) / (2*height)]
                    #print(pt)
                    if t_id in self.Q:
                        self.Q[t_id][0].append(pt)
                    else:
                        self.Q[t_id] = [[pt]]
                # print(self.Q)

                for i in self.Q:
                    if (len(self.Q[i][0])) == 8:
                        Q_np = np.array(self.Q[i], dtype=np.float32)
                        obs.append(Q_np)
                        Q_d = Q_np[:, 1:, 0:2] - Q_np[:, :-1, 0:2]
                        inp = torch.from_numpy(Q_d)
                        #print(i)
                        #print(inp)
                        if i == 0:
                            inp = (inp.to(device) - mean_end_effector.to(device)) / std_end_effector.to(device)
                        elif i == 1:
                            inp = (inp.to(device) - mean_arm.to(device)) / std_arm.to(device)
                        elif i == 2:
                            inp = (inp.to(device) - mean_probe_holder.to(device)) / std_probe_holder.to(device)
                        elif i == 4 :
                            inp = (inp.to(device) - mean_probe.to(device)) / std_probe.to(device)
                        else:
                            inp = (inp.to(device) - mean_ped.to(device)) / std_ped.to(device)
                        src_att = torch.ones((inp.shape[0], 1, inp.shape[1])).to(device)
                        start_of_seq = torch.Tensor([0, 0, 1]).unsqueeze(0).unsqueeze(1).repeat(inp.shape[0], 1, 1).to(
                            device)
                        dec_inp = start_of_seq
                        print("predicting trajectory")
                        for itr in range(12):
                            trg_att = subsequent_mask(dec_inp.shape[1]).repeat(dec_inp.shape[0], 1, 1).to(device)
                            if i == 0:
                                out = self.traj_endeffector(inp, dec_inp, src_att, trg_att)
                            elif i == 1:
                                out = self.traj_arm(inp, dec_inp, src_att, trg_att)
                            elif i == 2:
                                out = self.traj_probe_holder(inp, dec_inp, src_att, trg_att)
                            elif i == 4:
                                out = self.traj_probe(inp, dec_inp, src_att, trg_att)
                            else:
                                out = self.traj_ped(inp, dec_inp, src_att, trg_att)
                            dec_inp = torch.cat((dec_inp, out[:, -1:, :]), 1)
                        if i == 0:
                            preds_tr_b = (dec_inp[:, 1:, 0:2] * std_end_effector.to(device) + mean_end_effector.to(device)).detach().cpu().numpy().cumsum(1)+Q_np[:, -1:, 0:2]
                        elif i == 1:
                            preds_tr_b = (dec_inp[:, 1:, 0:2] * std_arm.to(device) + mean_arm.to(device)).detach().cpu().numpy().cumsum(1) + Q_np[:, -1:, 0:2]
                        elif i == 2:
                            preds_tr_b = (dec_inp[:, 1:, 0:2] * std_probe_holder.to(device) + mean_probe_holder.to(device)).detach().cpu().numpy().cumsum(1) + Q_np[:, -1:, 0:2]
                        elif i == 4:
                            preds_tr_b = (dec_inp[:, 1:, 0:2] * std_probe.to(device) + mean_probe.to(device)).detach().cpu().numpy().cumsum(1) + Q_np[:, -1:, 0:2]
                        else:
                            preds_tr_b = (dec_inp[:, 1:, 0:2] * std_ped.to(device) + mean_ped.to(device)).detach().cpu().numpy().cumsum(1) + Q_np[:, -1:, 0:2]
                        pr.append(preds_tr_b)
                        #pr = np.concatenate(pr, 0)
                        self.Q[i][0].pop(0)
            if len(boxes_to_print) > 0:
                boxes_xyxy = boxes_to_print.copy()
                ori_im = cv2.rectangle(ori_im, (int(window[0]), int(window[1])), (int(window[2]), int(window[3])), (0, 204, 204), 2)
                cv2.putText(ori_im, names[5], (int(window[0]), int(window[1]) - 10), 0, 1e-3 * height, (255, 0, 0), int((height + width) // 900))
                boxes_xyxy[:, 0] = boxes_to_print[:, 0] - boxes_to_print[:, 2] / 2.
                boxes_xyxy[:, 2] = boxes_to_print[:, 0] + boxes_to_print[:, 2] / 2.
                boxes_xyxy[:, 1] = boxes_to_print[:, 1] - boxes_to_print[:, 3] / 2.
                boxes_xyxy[:, 3] = boxes_to_print[:, 1] + boxes_to_print[:, 3] / 2.
                # TODO need to draw boxes
                # ori_im = cv2.rectangle(ori_im, (int(boxes_xyxy[2,0]), int(boxes_xyxy[2,1])), (int(boxes_xyxy[2,2]), int(boxes_xyxy[2,3])),
                #                        (0, 0, 255), 2)
                #ori_im = draw_boxes(ori_im, boxes_xyxy, cls_ids)
                for i in range(len(boxes_xyxy)):
                    ori_im = cv2.rectangle(ori_im, (int(boxes_xyxy[i, 0]), int(boxes_xyxy[i, 1])), (int(boxes_xyxy[i, 2]), int(boxes_xyxy[i, 3])), (0, 204, 204), 2)
                    position = (int(boxes_xyxy[i, 0]), int(boxes_xyxy[i, 1]) - 10)
                    if cls_ids[i] == 2:
                        position = (int(boxes_xyxy[i, 0]), int(boxes_xyxy[i, 3]) + 15)
                    cv2.putText(ori_im, names[cls_ids[i]], position, 0, 1e-3 * height, (255, 0, 0), int((height + width) // 900))
            co = (0, 255, 0)  # green
            cp = (0, 0, 255)  # red
            #print(preds_tr_b)
            for i in range(len(pr)):
                for j in range(11):
                    pp1 = (int(pr[i][0, j, 0]*width), int(pr[i][0, j, 1]*height))
                    pp2 = (int(pr[i][0, j+1, 0] * width), int(pr[i][0, j+1, 1] * height))
                    #ori_im = cv2.circle(ori_im, pp, 3, cp, -1)
                    ori_im = cv2.line(ori_im, pp1, pp2, cp, 2)
            for i in range(len(obs)):
                for j in range(7):
                    op1 = (int(obs[i][0, j, 0]*width), int(obs[i][0, j, 1]*height))
                    op2 = (int(obs[i][0, j+1, 0] * width), int(obs[i][0, j+1, 1] * height))
                    #ori_im = cv2.circle(ori_im, op, 3, co, -1)
                    ori_im = cv2.line(ori_im, op1, op2, co, 2)

            for i in range(len(pr)):
                for j in range(11, 0, -1):
                    pp = (int(pr[i][0, j, 0] * width), int(pr[i][0, j, 1] * height))
                    if pp[0] > window[0] and pp[1] > window[1] and pp[0] <window[2] and pp[1] < window[3]:
                        depth = get_depth(im_for_depth, self.model)
                        print("distance from camera")
                        print(depth[pp[1]][pp[0]])
                        if depth[pp[1]][pp[0]]<5 and depth[pp[1]][pp[0]]>4:
                            print("collision detected")
                            ori_im = cv2.rectangle(ori_im, (1330, 456), (1490, 660),(0, 0, 255), 4)
                            ori_im = cv2.putText(ori_im, 'Possible collision', (1160, 325), cv2.FONT_HERSHEY_SIMPLEX , 2, (0, 0, 255), 3, cv2.LINE_AA)
                            break

            # for i in range(11, 0, -1):
            #     if len(pr) >= 3:
            #         pp = (int(pr[0][0, i, 0] * width), int(pr[0][0, i, 1] * height))
            #         if pp[0] > window[0] and pp[1] > window[1] and pp[0] <window[2] and pp[1] < window[3]:
            #             print("collision detected")
            #             ori_im = cv2.rectangle(ori_im, (1330, 456), (1490, 660), (0, 0, 255), 4)
            #             ori_im = cv2.putText(ori_im, 'Possible collision', (1160, 325), cv2.FONT_HERSHEY_SIMPLEX , 2, (0, 0, 255), 3, cv2.LINE_AA)
            #             break

            cv2.imshow("test", ori_im)
            cv2.waitKey(1)
            # draw boxes for visualization
            # if len(outputs) > 0:
            #     bbox_tlwh = []
            #     bbox_xyxy = outputs[:, :4]
            #     identities = outputs[:, -1]
            #     ori_im = draw_boxes(ori_im, bbox_xyxy, identities)
            #
            #     for bb_xyxy in bbox_xyxy:
            #         bbox_tlwh.append(self.deepsort._xyxy_to_tlwh(bb_xyxy))
            #
            #     results.append((idx_frame - 1, bbox_tlwh, identities))

            end = time.time()

            # if self.args.display:
            #     cv2.imshow("test", ori_im)
            #     cv2.waitKey(1)

            if self.args.save_path:
                self.writer.write(ori_im)

            # save results
            write_results(self.save_results_path, results, 'mot')


            # logging
            # self.logger.info("time: {:.03f}s, fps: {:.03f}, detection numbers: {}, tracking numbers: {}" \
            #                  .format(end - start, 1 / (end - start), bbox_xywh.shape[0], len(outputs)))



def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--VIDEO_PATH", type=str, default="C:/Users/venny/Desktop/new data/new_video_3_Trim1.mp4")
    parser.add_argument("--config_detection", type=str, default="./configs/yolov3.yaml")
    parser.add_argument("--config_deepsort", type=str, default="./configs/deep_sort.yaml")
    # parser.add_argument("--ignore_display", dest="display", action="store_false", default=True)
    parser.add_argument("--display", action="store_false")
    parser.add_argument("--frame_interval", type=int, default=10)
    parser.add_argument("--display_width", type=int, default=800)
    parser.add_argument("--display_height", type=int, default=600)
    parser.add_argument("--save_path", type=str, default="./output/")
    parser.add_argument("--cpu", dest="use_cuda", action="store_false", default=True)
    parser.add_argument("--camera", action="store", dest="cam", type=int, default="-1")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = get_config()
    cfg.merge_from_file(args.config_detection)
    cfg.merge_from_file(args.config_deepsort)

    with VideoTracker(cfg, args, video_path=args.VIDEO_PATH) as vdo_trk:
        vdo_trk.run()
